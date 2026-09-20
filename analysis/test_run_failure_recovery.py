"""Regression tests for the two ways a run dies and becomes unreadable.

Both bugs were found from a real crashed run in results/, not by speculation:

1. `RemoteDisconnected` killed a live run at turn 58 of episode 6. It is a
   `ConnectionError` and an `http.client.HTTPException`, but NOT a
   `urllib.error.URLError`, so the retry filter let it straight through and one
   transient proxy hiccup terminated the whole run. A peer that closes the
   connection mid-response is the textbook transient fault.

2. The same run had 58 turns on disk but no summary (the summary is written
   only after the whole dataset loop returns), and `/api/run/<id>` answered
   404 -- so the Replays tab could not list its episodes and the per-run
   export failed. The data was on disk the whole time; the reader refused to
   read it.

Run:  python analysis/test_run_failure_recovery.py
"""
import http.client
import json
import os
import sys
import tempfile
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ["DRAWTLE_OVERLAY_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="drawtle-fail-cfg-"), "overlay.json")
os.environ["DRAWTLE_SETTINGS_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="drawtle-fail-cfg-"), "settings.json")
os.environ["DRAWTLE_CRED_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="drawtle-fail-cfg-"), "credentials.json")

from drawtle import models as MOD  # noqa: E402
from drawtle import runstate as RS  # noqa: E402
from web import server as S  # noqa: E402

PASS, FAIL = [], []


class _FlakyBackend(MOD.ModelBackend):
    """Raises N times, then succeeds. Records nothing to the network."""

    name = "flaky"
    key_env = ()

    def __init__(self, fail_n, exc, **kw):
        super().__init__("flaky-model", **kw)
        self.fail_n = fail_n
        self.exc = exc
        self.calls = 0

    def _post(self, messages, **kw):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise self.exc
        return {"choices": [{"message": {"content": '{"turn": 0, "step": 1}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3}}

    def _wrap(self, payload, lat):
        ch = payload["choices"][0]["message"]["content"]
        return MOD.ModelResponse(text=ch, prompt_tokens=5,
                                 completion_tokens=3, latency_s=lat)


def check(name, got, want, detail=""):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}"
          + ("" if ok else f"\n        want {want!r}, got {got!r} {detail}"))


def _drop_on_floor(attempts):
    """A `complete()` whose deadline-worker raises RemoteDisconnected N times.

    `_post_with_deadline` re-raises the worker's exception, so raising from
    `_post` is exactly the path the real run took.
    """
    b = _FlakyBackend(attempts, http.client.RemoteDisconnected(
        "Remote end closed connection without response"))
    b.max_retries = 4
    b.timeout_s = 5
    resp = b.complete([{"role": "user", "content": "go"}])
    return b, resp


def main():
    print("-- a dropped connection is retried, not fatal --")
    # The exact exception that killed the real run. It must be retried and the
    # call must eventually succeed; before the fix it propagated immediately.
    b, resp = _drop_on_floor(2)
    check("RemoteDisconnected is retried and the call succeeds",
          resp.text, '{"turn": 0, "step": 1}', f"calls={b.calls}")
    check("the retry actually retried (2 failures => 3 attempts)",
          b.calls, 3, f"calls={b.calls}")

    # The sibling connection-reset classes ride the same branch.
    for label, exc in (
        ("ConnectionResetError", ConnectionResetError("reset by peer")),
        ("ConnectionAbortedError", ConnectionAbortedError("aborted")),
        ("BadStatusLine", http.client.BadStatusLine("garbage")),
    ):
        b2 = _FlakyBackend(1, exc)
        ok = False
        try:
            r = b2.complete([{"role": "user", "content": "go"}])
            ok = r.text == '{"turn": 0, "step": 1}'
        except Exception as e:                       # noqa: BLE001
            print(f"        raised {type(e).__name__}: {e}")
        check(f"{label} is retried and the call succeeds", ok, True)

    # A fault that never clears still fails -- retries must not become an
    # infinite loop, and the caller sees the real exception.
    forever = _FlakyBackend(99, ConnectionResetError("always down"))
    forever.max_retries = 2
    raised = None
    try:
        forever.complete([{"role": "user", "content": "go"}])
    except Exception as e:                           # noqa: BLE001
        raised = type(e).__name__
    check("a permanent fault still raises after the retries are spent",
          raised, "ConnectionResetError", f"calls={forever.calls}")
    check("the retry budget was respected (max_retries=2 => 3 attempts)",
          forever.calls, 3, f"calls={forever.calls}")

    print("-- a run with turns but no summary is still readable --")
    tmp = tempfile.mkdtemp(prefix="drawtle-fail-res-")
    rid = "run-flaky-1"
    paths = RS.run_paths(tmp, rid, model="flaky-model")
    os.makedirs(os.path.dirname(paths["jsonl"]), exist_ok=True)
    # The on-disk state of a crashed run: a status record and turns, but the
    # summary was never written (it comes after the dataset loop).
    RS.mark_started(tmp, rid, {"model": "flaky-model", "backend": "flaky"})
    with open(paths["jsonl"], "w", encoding="utf-8") as fh:
        for ep, prog in ((0, True), (0, False), (1, True)):
            fh.write(json.dumps({"episode": ep, "turn": 1,
                                 "progressed": prog, "error_class": "ok",
                                 "prompt_keys": ["k"]}) + "\n")
    RS.mark_finished(tmp, rid, RS.STATUS_ERROR,
                     {"error": "RemoteDisconnected", "n_turns_written": 3})

    data, code = S.run_summary_json(tmp, rid)
    check("a summary-less run answers 200, not 404", code, 200, str(code))
    check("the partial view is flagged", bool(data and data.get("partial")),
          True, str(data and data.get("partial")))
    check("the status record's error is carried through",
          data and data.get("status"), "error",
          str(data and data.get("status")))
    check("episodes are recovered from the turn log",
          data and sorted(e["episode"] for e in data.get("episodes") or []),
          [0, 1], str(data and data.get("episodes")))
    check("per-episode progress is computed from written turns",
          data and {e["episode"]: e["progress_rate"]
                    for e in data.get("episodes") or []},
          {0: 0.5, 1: 1.0}, str(data and data.get("episodes")))
    # The whole-dataset aggregates must stay unknown, never zero.
    check("the run-level progress rate is unknown, not zero",
          data and data.get("progress_rate"), None,
          str(data and data.get("progress_rate")))
    check("vision is detected from the turn log",
          bool(data and data.get("is_vision")), True,
          str(data and data.get("is_vision")))

    # A run id with nothing on disk is still a clean 404 -- the degradation
    # must not turn a nonexistent run into an empty result.
    data2, code2 = S.run_summary_json(tmp, "run-that-never-was")
    check("a nonexistent run still 404s", code2, 404, str(code2))

    # And the HTML page path (the /run/<id> link) degrades the same way.
    body, hcode = S.run_html(tmp, rid)
    check("the run page renders for a summary-less run", hcode, 200,
          str(hcode))
    check("the run page says the run is not a result",
          "Not a result" in body, True, body[:120])

    print("-- a transient lock on the atomic publish does not kill the write --")
    # Windows raises Access Denied from os.replace when an antivirus or a
    # polling reader holds the destination for a few ms. That killed two real
    # runs (a container smoke run and a CI floor-check run) before the retry.
    # The lock is modelled by failing the first N replaces and then succeeding.
    import builtins
    import drawtle.runstate as RS_MOD
    real_replace = os.replace
    state = {"n": 0}

    def flaky_replace(src, dst):
        state["n"] += 1
        if state["n"] <= 3:
            raise PermissionError(13, "Access is denied")
        return real_replace(src, dst)

    p = os.path.join(tmp, "locked.json")
    with mock.patch.object(RS_MOD.os, "replace", flaky_replace):
        RS_MOD.atomic_write_json(p, {"saved": True})
    with open(p, encoding="utf-8") as fh:
        back = json.load(fh)
    check("a transient lock is retried and the write lands",
          back, {"saved": True}, f"attempts={state['n']}")
    check("the retry actually retried (3 transient failures)",
          state["n"], 4, f"attempts={state['n']}")
    check("no stray temp file was left behind",
          not any(f.startswith("locked.json.") and f.endswith(".tmp")
                  for f in os.listdir(tmp)), True, str(os.listdir(tmp)))

    # A permanent permission error must still surface -- the retry must never
    # turn a real failure into a silent success.
    state["n"] = 0

    def always_denied(src, dst):
        state["n"] += 1
        raise PermissionError(13, "Access is denied")

    p2 = os.path.join(tmp, "always-locked.json")
    raised = None
    with mock.patch.object(RS_MOD.os, "replace", always_denied):
        try:
            RS_MOD.atomic_write_json(p2, {"no": "good"})
        except PermissionError:
            raised = "PermissionError"
    check("a permanent permission error still raises",
          raised, "PermissionError", f"attempts={state['n']}")
    check("the permanent error exhausted the retry budget",
          state["n"], RS_MOD._REPLACE_ATTEMPTS, f"attempts={state['n']}")

    print("-- a probe sweep cannot pin the panel's thread pool --")
    # probe_all is sequential and each dead host can take a full timeout. Twenty
    # providers at twelve seconds each is a minutes-long request that holds an
    # HTTP worker the whole way, and that starved every other route -- the
    # Analytics and Integrity views sat on "loading" until their render
    # watchdog tripped. The sweep is now wall-clock bounded.
    import drawtle.discovery as DSC

    class _Slow:
        """A provider whose probe always sleeps, so the bound is observable."""

        def __init__(self, want):
            self.want = want
            self.calls = 0

        def merged_providers(self):
            return {f"slow-{i}": {"models_url": f"http://invalid-{i}/v1/models",
                                  "auth": "bearer"}
                    for i in range(self.want)}

        def probe(self, name, spec, timeout=12.0):
            self.calls += 1
            # Longer than any sane per-call cap, but shorter than the total
            # budget, so the sweep must stop part-way rather than finish.
            time.sleep(0.4)
            return {"ok": True, "provider": name, "models": [], "n": 0}

    slow = _Slow(40)
    t0 = time.time()
    with mock.patch.object(DSC, "merged_providers", slow.merged_providers), \
            mock.patch.object(DSC, "probe", slow.probe):
        res = DSC.probe_all(timeout=12.0, budget=1.0)
    elapsed = time.time() - t0
    # A 1s budget against 40 providers that each take 0.4s must stop well
    # before the ~16s of pure work they represent -- that is the property.
    check("the sweep respects its wall-clock budget and does not run to "
          "completion when it is slow", elapsed < 10.0, True,
          f"elapsed={elapsed:.1f}s calls={slow.calls}")
    check("the sweep reports the providers it did not get to",
          any(r.get("error", "").startswith("skipped") for r in res), True,
          str([r.get("error") for r in res][:3]))
    check("the sweep still answered for the providers it reached",
          any(r.get("ok") for r in res), True, str(res[:1]))

    print()
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)}")
    for f in FAIL:
        print("  FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
