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
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Where the sandbox container sees the mounted results directory
#: (docker/docker-compose.yml mounts ../results -> /bench/results).
_CONTAINER_RESULTS = "/bench/results"

#: How many lines of a child's output to keep. Enough for a status line and a
#: stack trace; not enough to hold a whole run's stdout.
TAIL_LINES = 400

#: A run that produces no output at all still has to be killable, so the child
#: is polled rather than waited on.
_POLL_S = 0.4

#: Windows exit status for "terminated by Ctrl-C / Ctrl-Break".
WINDOWS_CTRL_C_EXIT = 0xC000013A

#: `bench.py run` prints `run_id   : <id>` immediately after resolving a blank
#: run id, which is the moment the job can adopt the child's real identity.
_RUN_ID_RE = re.compile(r"^run_id\s*:\s*(\S+)\s*$")


class Job:
    """One running bench process."""

    def __init__(self, job_id, argv, run_id, backend, model, logs_dir="logs"):
        self.job_id = job_id
        self.argv = argv
        self.run_id = run_id
        self.backend = backend
        self.model = model
        self.logs_dir = logs_dir
        self.started = time.time()
        self.started_iso = time.strftime("%Y-%m-%dT%H:%M:%S",
                                         time.localtime(self.started))
        self.finished = None
        self.returncode = None
        self._lock = threading.Lock()
        self._tail = []
        self.proc = None
        self.error = None
        self._logfh = None

    # -- output ----------------------------------------------------------

    def _append(self, line):
        """Keep the bounded tail AND append to the on-disk log.

        The tail is what the Logs tab shows live; the disk log is the
        permanent record, so a dashboard restart never loses the story of a
        run. One `logs/<run_id>.log` per run, plain text -- grep-able by the
        operator, crash-safe because we append with no buffering games.
        """
        line = line.rstrip("\n")
        with self._lock:
            self._tail.append(line)
            if len(self._tail) > TAIL_LINES:
                del self._tail[:len(self._tail) - TAIL_LINES]
            if self._logfh is not None:
                try:
                    self._logfh.write(line + "\n")
                    self._logfh.flush()
                except Exception:                  # noqa: BLE001 - must not kill the run
                    pass

    def tail(self):
        with self._lock:
            return list(self._tail)

    def adopt_run_id(self, run_id):
        """Adopt the child's real run id, printed right after it starts.

        A launch that leaves the run id blank has the CHILD choose one, so the
        supervisor is stuck with `(auto)` until the child says what it picked --
        and a stop clicked before that line arrives would otherwise record the
        interruption against a run called `(auto)` while the real run stays
        `started` forever. The id is validated before adoption: it reaches
        path construction in the stop path, so it must be a plain name.
        """
        if not run_id or self.run_id != "(auto)":
            return
        from . import guard as G
        if not G.safe_run_id(run_id):
            return
        with self._lock:
            self.run_id = run_id

    @property
    def log_path(self):
        """`<logs_dir>/<model>/<run_id>.log`.

        Grouped by model so a model's sessions sit together, the same way the
        results are. The run id still carries the identity; the folder is for
        finding things, not for naming them, so two runs of the same model can
        never collide and no log is ever overwritten by a re-run.

        `logs_dir` is configurable so a test can point it at a temporary
        directory. A suite that writes its logs into the repo's own `logs/` is
        a suite that has to clean them up again -- and a cleanup that fails
        leaves junk that looks like a real run.
        """
        from drawtle.runstate import slugify
        name = str(self.run_id) if (self.run_id and self.run_id != "(auto)") \
            else str(self.job_id)
        return os.path.join(self.logs_dir, slugify(self.model), name + ".log")

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

    def __init__(self, results_dir="results", logs_dir="logs"):
        self.results_dir = results_dir
        # Where run logs are written. Configurable for the same reason
        # results_dir is: a test must be able to point both at a temp directory
        # so it never writes into -- or has to clean up -- the repo's data.
        self.logs_dir = logs_dir
        self._jobs = {}
        self._lock = threading.Lock()

    def list(self):
        # The jobs list is the supervisor's heartbeat -- a dashboard polls it
        # whenever the jobs view is visible. Reaping here is what makes the
        # finished-job cleanup actually run (nothing else called `reap`).
        self.reap()
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.started, reverse=True)
        return [j.as_dict() for j in jobs]

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    # -- starting --------------------------------------------------------

    def build_cmd(self, spec):
        """Validate a spec and return the argv, WITHOUT spawning a process.

        `start` calls this and then spawns; the tests call it directly, so a
        command string can be checked without a child process and without a
        run, results file or log. The validation is identical either way --
        a preview is as strict as a launch, because a command that would fail
        at spawn time should not be previewable as if it would work.

        The argv is assembled here from explicit fields rather than from a
        client-supplied string, because a shell string from a browser is a
        remote-code-execution request with extra steps. Only the fields below
        can reach the command line, each is type-checked, and the process is
        never started through a shell.

        With `spec["sandbox"]` the run is executed inside the sandbox
        container instead of on the host: the command becomes
        `docker compose run` against docker/docker-compose.yml, and the
        dataset / results paths are rewritten to the container's mount points
        (`/bench/results/...`), so the artifacts the container writes land in
        the same results directory every other reader reads.
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

        # Provenance, separate from `mode` above (which is the memory-lag
        # experiment). A caller that does not say gets the honest default:
        # a mock run is a self-test, anything else is a real measurement.
        from drawtle import runstate as RS
        run_mode = str(spec.get("run_mode") or "").strip().lower()
        if run_mode not in RS.MODES:
            run_mode = RS.infer_mode(backend)

        try:
            lag = max(1, int(spec.get("lag") or 1))
            limit = max(0, int(spec.get("limit") or 0))
        except (TypeError, ValueError):
            raise ValueError("lag and limit must be whole numbers")

        run_id = str(spec.get("run_id") or "").strip() or None
        if run_id and not G.safe_run_id(run_id):
            raise ValueError("a run id may contain letters, digits, dot, dash "
                             "and underscore only")

        effort = spec.get("effort")
        if effort and effort not in ("minimal", "low", "medium", "high"):
            raise ValueError("effort must be minimal, low, medium or high")

        if spec.get("sandbox"):
            from drawtle import discovery as DSC
            from drawtle import sandbox as SBX
            ok, detail = SBX.docker_available()
            if not ok:
                raise ValueError(
                    f"the sandbox container cannot be used: {detail}")
            prov = (DSC.merged_providers() or {}).get(backend) or {}
            url = str(prov.get("url") or "")
            if any(host in url for host in
                   ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")):
                raise ValueError(
                    f"{backend} is a localhost/self-hosted provider. The sandbox "
                    f"container has no route to your machine (see "
                    f"docker/sandbox.md), so it cannot reach it -- run this "
                    f"provider on the host instead.")
            docker = shutil.which("docker") or "docker"
            cmd = [docker, "compose", "-f",
                   os.path.join(ROOT, "docker", "docker-compose.yml"),
                   "run", "--rm", "-T", "bench", "run",
                   "--backend", backend, "--model", model,
                   "--dataset", _CONTAINER_RESULTS + "/" + os.path.basename(dataset),
                   "--out-dir", _CONTAINER_RESULTS,
                   "--mode", mode, "--lag", str(lag),
                   "--run-mode", run_mode]
        else:
            # The job runs under SOMEWHERE's Python. `sys.executable` is the
            # right default (the interpreter serving this page), but it may be
            # a bare system install without the rasteriser or SDK extras that
            # the bench environment has. `DRAWTLE_PYTHON` lets the operator
            # point the control centre at the environment actually built for
            # running the bench (e.g. a venv with playwright/cairosvg), so a
            # run spawned from the UI never fails at turn 0 for "no
            # rasteriser".
            interpreter = os.environ.get("DRAWTLE_PYTHON") or sys.executable
            cmd = [interpreter, os.path.join(ROOT, "bench.py"), "run",
                   "--backend", backend, "--model", model,
                   "--dataset", dataset,
                   "--out-dir", self.results_dir,
                   "--mode", mode, "--lag", str(lag),
                   "--run-mode", run_mode]

        if limit:
            cmd += ["--limit", str(limit)]
        if run_id:
            cmd += ["--run-id", run_id]
        if effort:
            cmd += ["--effort", effort]
        if spec.get("frames"):
            if spec.get("sandbox"):
                cmd += ["--frames", _CONTAINER_RESULTS + "/frames"]
            else:
                cmd += ["--frames", os.path.join(self.results_dir, "frames")]
        if spec.get("navigate"):
            cmd += ["--navigate"]
        return cmd

    def start(self, spec):
        """Spawn a run. Returns the Job. Raises ValueError on a bad spec."""
        cmd = self.build_cmd(spec)
        backend = str(spec.get("backend") or "").strip()
        model = str(spec.get("model") or "").strip()
        run_id = str(spec.get("run_id") or "").strip() or None

        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id, cmd, run_id or "(auto)", backend, model,
                  logs_dir=self.logs_dir)
        try:
            # The log lives in a per-model folder, so the folder has to exist
            # before the handle is opened.
            os.makedirs(os.path.dirname(job.log_path), exist_ok=True)
            job._logfh = open(job.log_path, "a", encoding="utf-8")
        except OSError:
            job._logfh = open(os.devnull, "w")   # logging must never block a run

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
            try:
                for line in job.proc.stdout:
                    job._append(line)
                    m = _RUN_ID_RE.match(line.rstrip("\n"))
                    if m:
                        job.adopt_run_id(m.group(1))
            except Exception as e:                 # pragma: no cover - rare
                job._append(f"[supervisor] read error: {type(e).__name__}: {e}")
            job.returncode = job.proc.wait()
            job.finished = time.time()
            job._append(f"[supervisor] process exited with code {job.returncode}")
            # Reconcile a hard exit. A child killed by SIGKILL/OOM (POSIX) or by
            # the OS (Windows) never runs its own cleanup, so its status file can
            # be left pinned on `started` forever -- which every reader treats as
            # "still going". A non-zero exit whose status is still `started` is a
            # crash, not a finish; record it as such. Zero and the controlled
            # 130 (SIGINT) are left alone, because those paths write their own
            # terminal status.
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
                except Exception as e:             # pragma: no cover - rare
                    job._append(f"[supervisor] could not record the exit: {e}")
            # Artifact verification. A process that exits 0 MUST have left a
            # terminal status and a summary; one that did is a result, one that
            # did not is a ghost -- and real runs have been eaten by exactly
            # that ghost (2026-09-21: run dirs left bare, status/summary gone).
            # Warn loudly in the one place the operator reads, rather than
            # letting a missing run read as "never happened".
            if job.run_id != "(auto)" and job.returncode == 0:
                try:
                    from drawtle import runstate as RS
                    st = RS.status_of(self.results_dir, job.run_id)
                    if st.get("status") != RS.STATUS_SUCCESS:
                        job._append(
                            f"[supervisor] WARNING: process exited 0 but the run "
                            f"status is {st.get('status')!r} "
                            f"(expected 'success'). Artifacts may have been "
                            f"removed after the run; check "
                            f"{self.results_dir}/{job.run_id}/")
                except Exception:                  # pragma: no cover - rare
                    pass
        finally:
            if job._logfh is not None:
                try:
                    job._logfh.close()
                except OSError:
                    pass

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

        The run id is resolved first. The child picks one when the launch left
        it blank, so `(auto)` is only ever a placeholder; recording an
        interruption against a run named `(auto)` orphaned the real run in
        `started` forever.
        """
        try:
            from drawtle import runstate as RS
        except Exception:
            return False
        if job.run_id == "(auto)":
            found = self._resolve_run_id(job)
            if found:
                job.adopt_run_id(found)
        rid = job.run_id
        status = RS.status_of(self.results_dir, rid)
        if rid == "(auto)" or status not in (RS.STATUS_STARTED, RS.STATUS_UNKNOWN):
            return False                       # it finished on its own; leave it
        try:
            extra = {
                "n_turns_written": self._turns_written(rid),
                "note": "stopped from the control centre. The operating system "
                        "terminated the run before it could record its own "
                        "outcome, so the interruption is recorded on its behalf. "
                        "Any episodes that completed are in the checkpoint.",
                "resume_hint": f"--resume --run-id {rid}",
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
            RS.mark_interrupted(self.results_dir, rid, extra)
            job._append("[supervisor] run recorded as interrupted")
            return True
        except Exception as e:                 # pragma: no cover - rare
            job._append(f"[supervisor] could not record the interruption: {e}")
            return False

    def _resolve_run_id(self, job):
        """Find the run this job actually created, or None.

        The candidate pool is every run still marked `started` whose model and
        backend match the job's -- the child writes its status record before any
        model call, so a matching `started` run is unambiguously this job's.
        The newest wins, which only matters if a server restarted mid-job and
        two `started` runs of the same model exist (the old one then looks
        stalled; the new one is the one being stopped).
        """
        try:
            from drawtle import runstate as RS
            from drawtle import stats as ST
        except Exception:
            return None
        best = None
        try:
            for rid, run in ST.enumerate_runs(self.results_dir).items():
                st = run["record"] or {}
                if st.get("status") != RS.STATUS_STARTED:
                    continue
                if (st.get("model"), st.get("backend")) != (job.model, job.backend):
                    continue
                ts = st.get("started_at") or 0
                if best is None or ts > best[2]:
                    best = (rid, st, ts)
        except Exception:                     # pragma: no cover - defensive
            return None
        return best[0] if best else None

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
