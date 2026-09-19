"""Run supervisor: start a bench run as a child process, stream its output.

Why a subprocess and not an in-process thread
---------------------------------------------
Two reasons, and the second is the one that matters.

1. A run that takes an hour must not depend on a browser tab staying open, or
   on this server not being restarted. `bench.py run` already writes its own
   status file, JSONL and summary, so it is a complete program; re-implementing
   it inside a web request would mean two implementations of the same thing and
   one of them would drift.

2. **The run's provenance must not include the dashboard.** A result has to be
   reproducible by running the command shown in its own status record. If the
   server built the run in-process, the numbers would carry an implicit
   dependency on the HTTP layer -- and a reviewer could not re-run it.

So: this module constructs an argv, spawns it, keeps the tail of its output in
memory for the Logs tab, and nothing else. It never parses the model's output,
never scores anything, and never touches a results file. The `run_id` it reports
is the one the child chose, and the command line is stored so the run can be
reproduced outside the dashboard with a copy-paste.

Stopping a run, and why it is done the way it is
------------------------------------------------
On Windows there is no way to deliver a catchable interrupt to a console child.
`CTRL_BREAK_EVENT` does not raise `KeyboardInterrupt` in the child; it terminates
it at the OS level with status `0xC000013A`, **before any Python cleanup can
run**. Measured, not assumed: a run stopped that way leaves its status file
reading `started` forever, and every reader of this project treats `started` as
"still in progress" rather than as "stopped".

So on Windows a stop is performed from the *parent*: the status file is written
first, on the child's behalf, and only then is the process terminated. On POSIX
`SIGINT` raises `KeyboardInterrupt` in the child as normal and the child records
its own interruption, which remains the preferred path because the child knows
things the parent does not -- how many episodes it finished and where its
checkpoint is.

The parent-side write is deliberately narrow: it applies only when the run has
not already recorded a terminal status of its own, so a run that finished
between the click and the signal is never relabelled.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: How many lines of a child's output to keep. Enough for a status line and a
#: stack trace; not enough to hold a whole run's stdout.
TAIL_LINES = 400

#: A run that produces no output at all still has to be killable, so the child
#: is polled rather than waited on.
_POLL_S = 0.4

#: Windows exit status for "terminated by Ctrl-C / Ctrl-Break".
WINDOWS_CTRL_C_EXIT = 0xC000013A


class Job:
    """One running bench process."""

    def __init__(self, job_id, argv, run_id, backend, model):
        self.job_id = job_id
        self.argv = argv
        self.run_id = run_id
        self.backend = backend
        self.model = model
        self.started = time.time()
        self.started_iso = time.strftime("%Y-%m-%dT%H:%M:%S",
                                         time.localtime(self.started))
        self.finished = None
        self.returncode = None
        self._lock = threading.Lock()
        self._tail = []
        self.proc = None
        self.error = None

    # -- output ----------------------------------------------------------

    def _append(self, line):
        with self._lock:
            self._tail.append(line.rstrip("\n"))
            if len(self._tail) > TAIL_LINES:
                del self._tail[:len(self._tail) - TAIL_LINES]

    def tail(self):
        with self._lock:
            return list(self._tail)

    # -- state -----------------------------------------------------------

    @property
    def status(self):
        if self.proc is None:
            return "failed" if self.error else "starting"
        if self.proc.poll() is None:
            return "running"
        return "success" if self.returncode == 0 else "exited"

    def as_dict(self):
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "backend": self.backend,
            "model": self.model,
            "status": self.status,
            "started_iso": self.started_iso,
            "elapsed_s": round((self.finished or time.time()) - self.started, 1),
            "returncode": self.returncode,
            "tail": self.tail(),
            "command": " ".join(shlex.quote(a) for a in self.argv),
            "error": self.error,
        }


class Supervisor:
    """Owns every process this server started. One instance per server."""

    def __init__(self, results_dir="results"):
        self.results_dir = results_dir
        self._jobs = {}
        self._lock = threading.Lock()

    def list(self):
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.started, reverse=True)
        return [j.as_dict() for j in jobs]

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    # -- starting --------------------------------------------------------

    def start(self, spec):
        """Spawn a run. Returns the Job. Raises ValueError on a bad spec.

        The argv is assembled here from explicit fields rather than from a
        client-supplied string, because a shell string from a browser is a
        remote-code-execution request with extra steps. Only the fields below
        can reach the command line, each is type-checked, and the process is
        never started through a shell.
        """
        backend = str(spec.get("backend") or "").strip()
        model = str(spec.get("model") or "").strip()
        dataset = str(spec.get("dataset") or "").strip()
        if not backend:
            raise ValueError("a provider (backend) is required")
        if not model:
            raise ValueError("a model id is required")
        if not dataset:
            raise ValueError("a dataset is required")

        from . import guard as G

        # Only a provider that exists may be launched. Checked against the
        # registry, which the UI also reads, so a launch cannot name a backend
        # the runner would then fail to construct on turn one.
        if not G.backend_exists(backend):
            raise ValueError(f"no such provider {backend!r}; add it on the "
                             f"Providers tab first")

        mode = spec.get("mode") or "optimal"
        if mode not in ("optimal", "stale"):
            raise ValueError("mode must be 'optimal' or 'stale'")

        try:
            lag = max(1, int(spec.get("lag") or 1))
            limit = max(0, int(spec.get("limit") or 0))
        except (TypeError, ValueError):
            raise ValueError("lag and limit must be whole numbers")

        run_id = str(spec.get("run_id") or "").strip() or None
        if run_id and not G.safe_run_id(run_id):
            raise ValueError("a run id may contain letters, digits, dot, dash "
                             "and underscore only")

        cmd = [sys.executable, os.path.join(ROOT, "bench.py"), "run",
               "--backend", backend, "--model", model,
               "--dataset", dataset,
               "--out-dir", self.results_dir,
               "--mode", mode, "--lag", str(lag)]
        if limit:
            cmd += ["--limit", str(limit)]
        if run_id:
            cmd += ["--run-id", run_id]
        effort = spec.get("effort")
        if effort:
            if effort not in ("minimal", "low", "medium", "high"):
                raise ValueError("effort must be minimal, low, medium or high")
            cmd += ["--effort", effort]
        if spec.get("frames"):
            cmd += ["--frames", os.path.join(self.results_dir, "frames")]
        if spec.get("navigate"):
            cmd += ["--navigate"]

        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id, cmd, run_id or "(auto)", backend, model)

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"      # so the tail is live, not block-buffered
        try:
            job.proc = subprocess.Popen(
                cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
                # A new process group, so Stop kills the run without touching
                # this server. Without it, Ctrl-C semantics leak between them.
                start_new_session=(os.name != "nt"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                               if os.name == "nt" else 0))
        except OSError as e:
            job.error = f"could not start: {e}"
            with self._lock:
                self._jobs[job_id] = job
            return job

        with self._lock:
            self._jobs[job_id] = job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job

    def _pump(self, job):
        """Read the child's output into the bounded tail until it exits."""
        try:
            for line in job.proc.stdout:
                job._append(line)
        except Exception as e:                     # pragma: no cover - rare
            job._append(f"[supervisor] read error: {type(e).__name__}: {e}")
        job.returncode = job.proc.wait()
        job.finished = time.time()
        job._append(f"[supervisor] process exited with code {job.returncode}")
        # Reconcile a hard exit. A child killed by SIGKILL/OOM (POSIX) or by the
        # OS (Windows) never runs its own cleanup, so its status file can be left
        # pinned on `started` forever -- which every reader treats as "still
        # going". A non-zero exit whose status is still `started` is a crash, not
        # a finish; record it as such. Zero and the controlled 130 (SIGINT) are
        # left alone, because those paths write their own terminal status.
        if job.returncode not in (0, 130):
            try:
                from drawtle import runstate as RS
                if RS.status_of(self.results_dir, job.run_id) == RS.STATUS_STARTED:
                    RS.mark_finished(
                        self.results_dir, job.run_id, RS.STATUS_ERROR,
                        {"error": f"process exited with code {job.returncode} "
                                  f"and wrote no terminal status; the supervisor "
                                  f"recorded the failure on its behalf",
                         "returncode": job.returncode,
                         "stopped_by": "supervisor-crash-detect"})
                    job._append("[supervisor] child exited uncleanly; recorded "
                                "as error")
            except Exception as e:                 # pragma: no cover - rare
                job._append(f"[supervisor] could not record the exit: {e}")

    # -- stopping --------------------------------------------------------

    def kill(self, job_id):
        """Stop a run. Returns (ok, message).

        See the module docstring: on Windows the status file has to be written
        by this process, because the OS termination cannot be caught by the
        child. On POSIX the child records its own interruption.
        """
        job = self.get(job_id)
        if not job:
            return False, "no such job"
        if job.proc is None or job.proc.poll() is not None:
            return False, "that run has already finished"

        pre_marked = False
        if os.name == "nt":
            pre_marked = self._mark_interrupted(job)

        try:
            if os.name == "nt":
                job.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                job.proc.send_signal(signal.SIGINT)
        except Exception:
            try:
                job.proc.terminate()
            except Exception as e:
                return False, f"could not signal the process: {e}"

        for _ in range(25):                        # up to ~5s to exit cleanly
            if job.proc.poll() is not None:
                break
            time.sleep(0.2)
        else:
            try:
                job.proc.kill()
            except Exception as e:
                return False, f"could not stop the process: {e}"

        job.returncode = job.proc.poll()
        job.finished = time.time()
        # The child may have recorded its own interruption first (POSIX), or
        # exited so fast that the pre-mark is the only record (Windows). Either
        # way, verify rather than assume: if the status still says `started`,
        # mark it now, because leaving it is the one outcome that makes a
        # stopped run look like a running one.
        self._mark_interrupted(job, force_if_started=True)
        msg = ("stopped; recorded as interrupted" if pre_marked
               else "stopped; the run recorded its own interruption")
        return True, msg

    def _mark_interrupted(self, job, force_if_started=False):
        """Record the run as interrupted, unless it already ended by itself.

        Returns True when this call wrote the record.

        Two guards, and the difference between them is the whole point:

        * A run that recorded a TERMINAL status (`success`/`error`/
          `interrupted`) is left alone. A run that completed a moment before
          the signal must never be relabelled as stopped.
        * A run whose status is `unknown` -- which means the child was killed
          before it wrote ANY record, the normal case on Windows where the
          termination is uncatchable -- is written as `interrupted` here. This
          is the case that was missing: returning early on `unknown` left a
          stopped run with no record at all, and a run with no record is
          indistinguishable from one that never ran.
        """
        try:
            from drawtle import runstate as RS
        except Exception:
            return False
        status = RS.status_of(self.results_dir, job.run_id)
        if status not in (RS.STATUS_STARTED, RS.STATUS_UNKNOWN):
            return False                       # it finished on its own; leave it
        try:
            extra = {
                "n_turns_written": self._turns_written(job.run_id),
                "note": "stopped from the control centre. The operating system "
                        "terminated the run before it could record its own "
                        "outcome, so the interruption is recorded on its behalf. "
                        "Any episodes that completed are in the checkpoint.",
                "resume_hint": f"--resume --run-id {job.run_id}",
                "stopped_by": "control-centre",
            }
            if status == RS.STATUS_UNKNOWN:
                # Nothing was ever written, so there is no prior record to
                # amend -- mark_finished would synthesise a minimal one, which
                # is correct but loses the fact that this *was* started. So the
                # fields the runner would have written are supplied here.
                extra.update({
                    "backend": job.backend,
                    "model": job.model,
                    "started_at": job.started,
                    "started_iso": job.started_iso,
                    "status_before_stop": None,
                })
            RS.mark_interrupted(self.results_dir, job.run_id, extra)
            job._append("[supervisor] run recorded as interrupted")
            return True
        except Exception as e:                 # pragma: no cover - rare
            job._append(f"[supervisor] could not record the interruption: {e}")
            return False

    def _turns_written(self, run_id):
        """How many turn records reached the log before the stop."""
        try:
            from drawtle import runstate as RS
            path = RS.run_paths(self.results_dir, run_id)["jsonl"]
            if not os.path.exists(path):
                return 0
            records, _rep = RS.scan_jsonl(path)
            return len(records)
        except Exception:
            return None                        # unknown, reported as such

    def reap(self, max_age_s=6 * 3600):
        """Drop finished jobs from memory once they are old.

        There is nothing to clean up on disk -- the run wrote its own artifacts
        -- so this is only about the server not accumulating Job objects over a
        long session.
        """
        now = time.time()
        with self._lock:
            gone = [jid for jid, j in self._jobs.items()
                    if j.finished and (now - j.finished) > max_age_s]
            for jid in gone:
                del self._jobs[jid]
        return len(gone)
