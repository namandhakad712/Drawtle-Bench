"""Run lifecycle: status, atomic writes, checkpointing, and log health.

Why this exists
---------------
A benchmark that only knows how to finish cleanly is not a benchmark you can
trust with a paid API key. Three failure modes matter, and none of them are
hypothetical:

1. **The process dies.** Ctrl-C, a provider 500 on turn 400 of 500, a laptop
   sleeping. Before this module a killed run left a partial JSONL with no
   record that it was partial, and a reader had no way to tell it apart from a
   short run. The reference implementation for this (Inspect AI) writes a log
   record even on error and tells consumers to check `status == "success"`
   before analysing anything. We adopt the same rule.

2. **The run is interrupted and must resume.** Re-running from scratch
   re-spends money on episodes that already succeeded. We checkpoint each
   finished episode to a sidecar so a resumed run skips them.

3. **The log's shape changes under you.** Records are written incrementally and
   the process can die mid-line. A half-written final line is the normal case,
   not an exotic one, and it silently corrupts a strict parser.

Design rules
------------
* **A run has a status at all times**, written before any work happens
  (`started`) and overwritten at the end (`success` / `error` / `interrupted`).
  A run whose status file says `started` and whose process is gone is
  *unfinished*, and every reader is told so.
* **Every write is atomic** — temp file in the same directory, then
  `os.replace`. A reader never observes a partial file.
* **A malformed line is reported, never skipped silently.** Skipping turns a
  corrupt log into a run with fewer turns than it really had, which is a wrong
  number rather than a loud failure.
* **Status is advisory, not load-bearing.** A missing status file (an old run
  from before this module existed) degrades to `unknown`, never to `success`.
"""
import json
import os
import time

STATUS_STARTED = "started"
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_INTERRUPTED = "interrupted"
STATUS_UNKNOWN = "unknown"

#: Statuses that mean "this run's numbers are complete and safe to analyse".
#: Note that `unknown` is NOT in here: a run that predates status tracking may
#: well be fine, but we cannot prove it, and the paper's whole claim rests on
#: numbers meaning what they say.
TERMINAL_OK = (STATUS_SUCCESS,)
TERMINAL_BAD = (STATUS_ERROR, STATUS_INTERRUPTED)


def run_paths(out_dir, run_id):
    """All the files a run owns, in one place.

    Centralised because these names were previously spelled out at each call
    site, and a typo in one of them produces an empty-but-valid-looking
    artifact rather than an error.
    """
    base = os.path.join(out_dir, run_id)
    return {
        "jsonl": base + ".jsonl",
        "summary": base + ".summary.json",
        "status": base + ".status.json",
        "checkpoint": base + ".done.json",
        "report": base + ".html",
    }


def atomic_write_json(path, obj):
    """Write JSON so a concurrent reader sees either the old file or the new.

    `open(path, "w")` truncates immediately, so there is a window in which the
    file exists and is empty. A dashboard polling the results directory during
    a run hits that window routinely; here it cannot.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def read_status(out_dir, run_id):
    """Read a run's status record, or a synthetic `unknown` if there is none."""
    p = run_paths(out_dir, run_id)["status"]
    if not os.path.exists(p):
        return {"run_id": run_id, "status": STATUS_UNKNOWN,
                "note": "no status file (run predates status tracking, or was "
                        "started outside this tool)"}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        # An unreadable status is itself a finding. Do not pretend it is fine.
        return {"run_id": run_id, "status": STATUS_UNKNOWN,
                "note": f"status file unreadable: {exc}"}


def mark_started(out_dir, run_id, meta):
    rec = dict(meta or {})
    rec.update({"run_id": run_id, "status": STATUS_STARTED,
                "started_at": time.time(),
                "started_iso": _iso(time.time())})
    return atomic_write_json(run_paths(out_dir, run_id)["status"], rec)


def mark_finished(out_dir, run_id, status, extra=None, started_at=None):
    """Record the terminal status. `success` is only ever passed explicitly.

    `started_at` should be the start of the CURRENT process, not of the original
    run. Summing across a resume would report a wallclock that includes the time
    the run spent dead, which reads as a slow model rather than an interrupted
    one; the caller passes the original in as `history_s` if it wants the total.
    """
    rec = read_status(out_dir, run_id)
    if rec.get("status") == STATUS_UNKNOWN:
        # No prior record -- build a minimal one rather than losing the outcome.
        rec = {"run_id": run_id}
    now = time.time()
    rec.update({"status": status, "finished_at": now, "finished_iso": _iso(now)})
    if started_at is not None:
        rec["wallclock_s"] = round(now - started_at, 1)
    rec.update(extra or {})
    return atomic_write_json(run_paths(out_dir, run_id)["status"], rec)


def mark_interrupted(out_dir, run_id, extra=None):
    """Called from the Ctrl-C path. Distinct from `error`: the caller asked."""
    return mark_finished(out_dir, run_id, STATUS_INTERRUPTED, extra)


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


# --------------------------------------------------------------------------
# checkpointing
# --------------------------------------------------------------------------

def load_done(out_dir, run_id):
    """Episode indices already completed, from the checkpoint sidecar.

    Keyed by manifest hash by the caller: resuming a run against a *different*
    dataset must not reuse episodes, because `episode: 3` means a different maze
    in each. The hash check lives in the caller where both are in scope.
    """
    p = run_paths(out_dir, run_id)["checkpoint"]
    if not os.path.exists(p):
        return {}, None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except Exception:
        return {}, None
    done = {int(k): v for k, v in (blob.get("done") or {}).items()}
    return done, blob.get("dataset_hash")


def save_done(out_dir, run_id, done, dataset_hash):
    return atomic_write_json(run_paths(out_dir, run_id)["checkpoint"],
                             {"run_id": run_id, "dataset_hash": dataset_hash,
                              "done": {str(k): v for k, v in sorted(done.items())}})


# --------------------------------------------------------------------------
# log health
# --------------------------------------------------------------------------

def scan_jsonl(path):
    """Parse a run log, reporting rather than hiding damage.

    Returns (records, report). `report` carries enough to decide whether the
    log is trustworthy:

      n_lines        lines physically present
      n_records      records successfully parsed
      bad_lines      [] or [(line_no, reason)] for every line that did not parse
      truncated_tail True when the LAST line is the broken one, which is the
                     signature of a process killed mid-write rather than of a
                     genuinely corrupt file
      bytes          file size

    A strict reader must refuse to silently drop `bad_lines`. The default
    `read_jsonl` below raises instead; callers that want the tolerant path ask
    for it explicitly.
    """
    records = []
    bad = []
    n_lines = 0
    if not os.path.exists(path):
        return [], {"n_lines": 0, "n_records": 0, "bad_lines": [],
                    "truncated_tail": False, "bytes": 0, "exists": False}
    size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if not line.strip():
                continue
            n_lines += 1
            try:
                records.append(json.loads(line))
            except Exception as exc:
                bad.append((i, f"{type(exc).__name__}: {exc}"))
    return records, {
        "n_lines": n_lines,
        "n_records": len(records),
        "bad_lines": bad,
        "truncated_tail": bool(bad) and bad[-1][0] == n_lines,
        "bytes": size,
        "exists": True,
    }


def read_jsonl(path, strict=True):
    """Read a run log.

    `strict=True` (default) raises on a malformed line. This is deliberate: a
    tolerant reader that skips bad lines will happily compute a progress rate
    over 480 of 500 turns and report it with a confidence interval, and nothing
    in the output says a fifth of the evidence went missing. If you want the
    tolerant behaviour, ask for it and handle the report.
    """
    records, rep = scan_jsonl(path)
    if strict and rep["bad_lines"]:
        first = rep["bad_lines"][0]
        hint = (" The last line is the broken one, which is what a process "
                "killed mid-write looks like -- re-run with --resume to "
                "continue from the checkpoint." if rep["truncated_tail"] else "")
        raise ValueError(
            f"{path}: {len(rep['bad_lines'])} malformed line(s), first at line "
            f"{first[0]} ({first[1]}).{hint}")
    return records


def status_of(out_dir, run_id):
    """Convenience: the status string alone."""
    return read_status(out_dir, run_id).get("status", STATUS_UNKNOWN)
