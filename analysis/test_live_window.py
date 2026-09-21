"""Tests for the live window: per-turn stream, reconstruction, and the routes.

Three properties carry the feature:

1. **The reconstructed state is byte-accurate.** `liveviz.frame_svg` must
   reproduce the exact SVG the runner sent (its SHA-256 equals the recorded
   `frame_hash`), and the frame served from the cache must be the model's own
   bytes. A live view that drew a different maze would be a different question.

2. **Turns are visible while the run is alive.** Records are written per turn
   (not per episode), so `/api/live/<run>` shows turn N while turn N+1 is still
   being answered. This is the fix for "nothing in the JSONL" -- a slow vision
   model used to leave a run looking dead for the duration of an episode.

3. **Stopping a run records the REAL run id**, never `(auto)` -- the bug that
   orphaned a live run in `started` forever while an artifact called `(auto)`
   said "interrupted".

Isolation: everything runs in a temp results dir; nothing writes into the repo.
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_TMP_CFG = tempfile.mkdtemp(prefix="drawtle-live-cfg-")
os.environ["DRAWTLE_SETTINGS_FILE"] = os.path.join(_TMP_CFG, "settings.json")
os.environ["DRAWTLE_OVERLAY_FILE"] = os.path.join(_TMP_CFG, "overlay.json")
os.environ["DRAWTLE_CRED_FILE"] = os.path.join(_TMP_CFG, "credentials.json")

from drawtle import dataset as D                      # noqa: E402
from drawtle import frames as F                       # noqa: E402
from drawtle import maze as M                         # noqa: E402
from drawtle import models as MOD                     # noqa: E402
from drawtle import runstate as RS                    # noqa: E402
from drawtle import runner as RUN                     # noqa: E402
from drawtle import liveviz as LVZ                    # noqa: E402
from web import live as LV                            # noqa: E402
from web import server as S                           # noqa: E402
from web import supervisor as SUP                     # noqa: E402

PORT = 8494
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = [], []
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"  {mark}  {name}"
    if not cond and detail:
        line += f"\n         {detail}"
    print(line)


def req(path, timeout=30):
    try:
        with _OPENER.open(BASE + path, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


class _VisBackend(MOD.ModelBackend):
    """A vision-capable fake: answers valid actions, records nothing."""

    name = "vissys"

    def __init__(self, model="vis-1", **kw):
        super().__init__(model, **kw)

    def _post(self, messages, **kw):
        return {"choices": [{"message": {"content": '{"turn": 0, "step": 1}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}}

    def _wrap(self, payload, lat):
        ch = payload["choices"][0]["message"]["content"]
        return MOD.ModelResponse(text=ch, prompt_tokens=100, completion_tokens=20,
                                 latency_s=lat, token_source="measured")


def _fake_rasterise(svg, out_path):
    """Deterministic fake PNG: no real rasteriser needed for these tests."""
    with open(out_path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 48)
    return out_path


def _vision_run(results_dir, run_id="vis-run-1"):
    """One real vision episode against a fake backend. Returns the records."""
    manifest = D.build_manifest(count=2, sizes=(9,), seed=11)
    frames_dir = os.path.join(results_dir, "frames")
    old = dict(MOD._CLASSES)
    MOD._CLASSES["vissys"] = _VisBackend
    real_f = F.rasterize
    F.rasterize = _fake_rasterise
    try:
        backend = MOD.make_backend("vissys", "vis-1")
        runner = RUN.Runner(backend, config={"max_turns": 14}, frame_dir=frames_dir,
                            run_id=run_id, navigate=True)
        spec = manifest["mazes"][0]
        turns, _summ = runner.run_episode(spec, D.make_maze(spec))
    finally:
        F.rasterize = real_f
        MOD._CLASSES.clear()
        MOD._CLASSES.update(old)
    return manifest, turns


def test_reconstruction_is_byte_accurate():
    """liveviz.frame_svg reproduces the runner's exact SVG -- same hash.

    The record's `frame_hash` is the SHA-256 of the SVG the model was sent; if
    the reconstruction drifts (wrong rotation, wrong camera, wrong maze), the
    hashes stop matching and the frame served would be a different question.
    """
    res = tempfile.mkdtemp(prefix="drawtle-live-acc-")
    manifest, turns = _vision_run(res)
    spec = manifest["mazes"][0]
    maze = D.make_maze(spec)
    cell = maze.entry
    checks = 0
    bad = []
    for rec in turns:
        if not rec.get("frame_hash"):
            continue
        svg = LVZ.frame_svg(spec, rec.get("rotation_deg") or 0, True,
                            cell, rec.get("true_heading") or 0)
        h = hashlib.sha256(svg.encode("utf-8")).hexdigest()[:16]
        if h != rec["frame_hash"]:
            bad.append((rec["turn"], h, rec["frame_hash"]))
        p = os.path.join(res, "frames", rec["frame_hash"] + ".png")
        if not os.path.exists(p):
            bad.append((rec["turn"], "no-png", rec["frame_hash"]))
        checks += 1
        cell = tuple(rec.get("applied_cell") or cell)
    check("reconstructed SVG hash == recorded frame_hash (every turn)",
          checks > 0 and not bad, f"checks={checks} bad={bad[:3]}")
    check("frame cache carries every recorded hash", checks > 0, f"checks={checks}")


def test_thinking_svg_draws_and_degrades():
    """The state diagram renders, and a garbage answer must not break it."""
    spec = {"idx": 0, "size": 9, "pair": "NW", "seed": 5}
    svg = LVZ.thinking_svg(spec, 90, False, (4, 4), 180,
                           {"turn": 90, "step": 1}, {"turn": -90, "step": 1},
                           error_class="ok", applied_cell=(4, 4))
    check("thinking svg is a document", svg.startswith("<svg") and "polygon" in svg,
          svg[:80])
    check("thinking svg carries the legend",
          "optimal" in svg and "model move" in svg)
    # Broken model output: no crash, still a diagram.
    svg2 = LVZ.thinking_svg(spec, 0, True, (2, 2), 0, None, None,
                            error_class="invalid", applied_cell=None)
    check("thinking svg survives a None move", svg2.startswith("<svg"),
          svg2[:60])
    svg3 = LVZ.thinking_svg(spec, 225, True, (2, 2), 0,
                            {"turn": "bogus", "step": 1}, {"turn": 7, "step": 0},
                            error_class="invalid")
    check("thinking svg survives a malformed move", svg3.startswith("<svg"),
          svg3[:60])


def _write_run_dir(results_dir, run_id, manifest, turns, navigate=True):
    """Materialise what a real run leaves behind: status + jsonl + frames."""
    rs_i = os.path.join(results_dir, "mock", run_id)
    os.makedirs(rs_i, exist_ok=True)
    RS.mark_started(results_dir, run_id, {
        "model": "vis-1", "backend": "vissys",
        "dataset_hash": manifest["hash"], "navigate": navigate,
    })
    jsonl = os.path.join(rs_i, "run.jsonl")
    with open(jsonl, "w", encoding="utf-8") as fh:
        for rec in turns:
            rec = dict(rec)
            rec["run_id"] = run_id
            rec["model"] = "vis-1"
            fh.write(json.dumps(rec) + "\n")
    return jsonl


def test_turns_stream_payload():
    """The feed rebuilds frame + svg + cell per turn and slices by offset."""
    res = tempfile.mkdtemp(prefix="drawtle-live-feed-")
    manifest, turns = _vision_run(res)
    _write_run_dir(res, "vis-run-1", manifest, turns)
    # The manifest must be discoverable by hash -- write it next to the run.
    D.save_manifest(manifest, os.path.join(res, "dataset-x.json"))

    data, code = LV.turns_stream(res, "vis-run-1", 0)
    check("turns_stream answers 200", code == 200, str(code))
    check("feed reports totals", data["total_turns"] == len(turns)
          and data["total_episodes"] == 1, str(data["total_turns"]))
    t0 = data["turns"][0]
    check("feed marks the run's dataset", data["dataset"]["name"] == "dataset-x.json",
          str(data["dataset"]))
    check("frame served from the cache is the model's bytes",
          t0["frame"] is not None and t0["has_frame"]
          and base64.b64decode(t0["frame"].split(",", 1)[1]) ==
          b"\x89PNG\r\n\x1a\n" + b"\x00" * 48)
    check("thinking svg included", bool(t0["svg"]) and t0["svg"].startswith("<svg"))
    check("cell chain starts at the entry",
          t0["cell"] == list(D.make_maze(manifest["mazes"][0]).entry),
          str(t0["cell"]))
    check("raw answer and expected action carried",
          t0["raw_model_text"] == '{"turn": 0, "step": 1}'
          and isinstance(t0["optimal_action"], dict))
    check("timestamp present", isinstance(t0["ts"], (int, float)))
    check("offset slicing returns only new turns",
          len(LV.turns_stream(res, "vis-run-1", len(turns))[0]["turns"]) == 0
          and len(LV.turns_stream(res, "vis-run-1", 1)[0]["turns"]) == len(turns) - 1)

    data2, code2 = LV.turns_stream(res, "no-such-run", 0)
    check("unknown run is a 404", code2 == 404 and "not found" in data2["error"],
          str(code2))


def test_turns_stream_degrades_without_manifest():
    """A deleted/absent manifest must not break the view -- no svg, no crash."""
    res = tempfile.mkdtemp(prefix="drawtle-live-noman-")
    manifest, turns = _vision_run(res)
    _write_run_dir(res, "vis-run-2", manifest, turns)   # no manifest file written
    data, code = LV.turns_stream(res, "vis-run-2", 0)
    check("no manifest -> still 200", code == 200, str(code))
    t0 = data["turns"][0]
    check("no manifest -> frame still served from cache", t0["has_frame"] is True)
    check("no manifest -> svg gracefully absent", t0["svg"] is None)


def test_supervisor_adopts_real_run_id():
    """A stop writes the child's real id, never `(auto)`."""
    work = tempfile.mkdtemp(prefix="drawtle-live-sup-")
    res = os.path.join(work, "results")
    logs = os.path.join(work, "logs")
    os.makedirs(res)
    os.makedirs(logs)
    RS.mark_started(res, "real-run-1", {"model": "mock", "backend": "mock",
                                        "dataset_hash": "sha256:0000000000000000"})

    sup = SUP.Supervisor(results_dir=res, logs_dir=logs)
    job = SUP.Job("job123", ["python", "bench.py", "run"], "(auto)", "mock", "mock",
                  logs_dir=logs)
    ok = sup._mark_interrupted(job)
    st = RS.read_status(res, "real-run-1")
    check("interruption resolved to the real run id", ok and job.run_id == "real-run-1"
          and st["status"] == RS.STATUS_INTERRUPTED, f"{job.run_id} {st}")
    check("no (auto) artifact was written",
          not os.path.exists(os.path.join(res, "(auto).status.json")))
    job.adopt_run_id("../../etc")
    check("adoption refuses a path id", job.run_id == "real-run-1", job.run_id)
    j2 = SUP.Job("j2", ["a"], "(auto)", "m", "m")
    j2.adopt_run_id("ok-2")
    check("adoption accepts a plain id", j2.run_id == "ok-2", j2.run_id)
    j2.adopt_run_id("bad/id")
    check("adoption ignores an unsafe id", j2.run_id == "ok-2", j2.run_id)


# -------------------------------------------------------------- server level --

def _cli_run(results_dir, run_id, manifest_path, limit=2):
    import subprocess
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "bench.py"), "run",
         "--backend", "mock", "--model", "mock", "--mode", "optimal",
         "--dataset", manifest_path, "--out-dir", results_dir,
         "--run-id", run_id, "--limit", str(limit), "--navigate",
         "--run-mode", "test"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise SystemExit(f"mock run failed: {r.stdout}\n{r.stderr}")


def test_routes():
    work = tempfile.mkdtemp(prefix="drawtle-live-http-")
    results_dir = os.path.join(work, "results")
    logs_dir = os.path.join(work, "logs")
    os.makedirs(results_dir)
    os.makedirs(logs_dir)
    # The preset ladder, so two runs land on two DIFFERENT datasets.
    D.write_preset_manifests(results_dir)
    _cli_run(results_dir, "lv-on-20", os.path.join(results_dir, "dataset-20.json"), 2)
    _cli_run(results_dir, "lv-on-50", os.path.join(results_dir, "dataset-50.json"), 2)

    sup = SUP.Supervisor(results_dir=results_dir, logs_dir=logs_dir)
    httpd, sup = S.make_server(results_dir, "127.0.0.1", PORT,
                               supervisor=sup, logs_dir=logs_dir)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.6)
    try:
        st, body = req("/api/live")
        check("/api/live lists the recent runs",
              st == 200 and any(r["run_id"] == "lv-on-20" for r in body["live"]),
              str(body)[:200])
        rid = body["live"][0]["run_id"]
        st2, d = req(f"/api/live/{rid}?offset=0", timeout=60)
        check("live stream serves turns over HTTP",
              st2 == 200 and len(d["turns"]) > 0 and d["turns"][0]["ts"],
              f"{st2} {str(d)[:160]}")
        check("live stream reports the run's dataset by hash",
              d["dataset"] and d["dataset"]["name"].startswith("dataset-"),
              str(d["dataset"]))
        check("live stream answer/expected present",
              d["turns"][0]["raw_model_text"] != ""
              and d["turns"][0]["optimal_action"] is not None)
        st3, d3 = req(f"/api/live/{rid}?offset={d['offset']}")
        check("offset past the end returns no turns",
              st3 == 200 and not d3["turns"], str(st3))

        st4, lb = req("/api/leaderboard", timeout=60)
        st5, lb20 = req("/api/leaderboard?dataset=" + _hash_of("dataset-20.json",
                                                                results_dir))
        check("leaderboard mixes only with no filter", st4 == 200 and len(lb["rows"]) >= 2,
              f"{st4} n={len(lb['rows'])}")
        check("dataset filter keeps only that dataset's runs",
              st5 == 200 and len(lb20["rows"]) == 1
              and lb20["rows"][0]["run_id"] == "lv-on-20"
              and lb20["rows"][0]["dataset_hash"]
              == next(x["hash"] for x in lb20["datasets"]
                      if x["name"] == "dataset-20.json"),
              f"{st5} n={len(lb20['rows'])} {str(lb20['rows'])[:160]}")
        check("bad dataset hash is a 400",
              req("/api/leaderboard?dataset=not%20a%20hash!")[0] == 400)
        st6, runs = req("/api/runs")
        row = next((r for r in runs["runs"] if r["run_id"] == "lv-on-20"), None)
        check("runs list carries dataset_hash", row is not None and row["dataset_hash"],
              str(row))
        # A started run appears as LIVE in /api/live.
        RS.mark_started(results_dir, "lv-started", {
            "model": "mock", "backend": "mock",
            "dataset_hash": _hash_of("dataset-20.json", results_dir),
            "started_at": time.time()})
        st7, b7 = req("/api/live")
        row7 = next((r for r in b7["live"] if r["run_id"] == "lv-started"), None)
        check("a started run is flagged live", row7 is not None and row7["is_live"] is True)
    finally:
        httpd.shutdown()
        httpd.server_close()


def _hash_of(name, results_dir):
    with open(os.path.join(results_dir, name), "r", encoding="utf-8") as fh:
        return json.load(fh)["hash"]


def main():
    print("unit:")
    test_reconstruction_is_byte_accurate()
    test_thinking_svg_draws_and_degrades()
    test_turns_stream_payload()
    test_turns_stream_degrades_without_manifest()
    test_supervisor_adopts_real_run_id()
    print("server:")
    test_routes()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:")
        for n in FAIL:
            print(" -", n)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
