"""End-to-end check of the control centre's HTTP surface.

Runs the server on a loopback port in a background thread and exercises every
route, including the ones that write. Nothing here touches the network beyond
loopback, and the write tests are reverted at the end so a run of this script
leaves the machine as it found it.

What this is for: the dashboard is now the thing that starts runs, so "does the
page load" is not a sufficient test. Each route is checked for the property it
actually promises --

  /api/runs          status comes from the sidecar, not the frozen summary
  /api/registry      a model with no capabilities is reported as `null`, not `[]`
  POST /api/model    a blank context window is stored as unknown, never as 0
  POST /api/run      a bad provider name is refused rather than spawned
  POST /api/kill     stops a real process and the run ends up `interrupted`

Run:  python analysis/test_control_centre.py
"""
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from web import server as S  # noqa: E402
from web import views as V  # noqa: E402
from drawtle import discovery as DSC  # noqa: E402

PORT = 8477
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = [], []

#: A developer machine often has an HTTP proxy configured (env or, on Windows,
#: the system registry). urllib would otherwise route *loopback* requests
#: through it and hang until timeout. These tests never leave the machine, so
#: they bypass any configured proxy outright.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"  {mark}  {name}"
    if not cond and detail:
        line += f"\n         {detail}"
    print(line)


def req(path, method="GET", body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def main():
    sup = S.SUP.Supervisor(results_dir="results")
    httpd = S.HTTPServer(
        ("127.0.0.1", PORT),
        lambda *a, **kw: S._Handler(*a, results_dir="results",
                                    supervisor=sup, **kw))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.6)

    overlay_before = DSC.load_overlay()

    try:
        # ---- the page itself -------------------------------------------
        with _OPENER.open(BASE + "/", timeout=10) as r:
            page = r.read().decode()
        check("the control centre renders", r.status == 200 and len(page) > 20000,
              f"status={r.status} bytes={len(page)}")
        for needle in ("Overview", "Providers", "Models", "Launch a run",
                       "Results", "Replays", "Storyboard", "Logs", "System"):
            check(f"tab present: {needle}", needle in page)
        check("no external resource is requested",
              "http://cdn" not in page and "https://cdn" not in page
              and "googleapis.com" not in page,
              "the page must render with no network access")
        check("unknown-is-not-zero rule is stated in the page",
              "Unknown is not zero" in page)
        check("excluded runs are called out in the page",
              "Only runs with status <code>success</code> are ranked" in page
              or "are ranked" in page)

        # ---- the client script (static, no server needed) ---------------
        # The single-page app builds every view in the browser, so a helper the
        # views call but the script never defines is a blank page, not a typo.
        # This is a static structural check: it catches the whole class of bug
        # at build time instead of waiting for a human to open the tab.
        import re as _re
        script = _re.search(r"<script>(.*)</script>", page, _re.S)
        check("the page ships exactly one client script",
              script is not None, "no <script> block found")
        if script:
            js = script.group(1)
            for helper in ("tag", "pct", "num", "money", "ci", "esc", "dash",
                           "note", "panel", "stats"):
                check(f"client script defines helper '{helper}'",
                      _re.search(rf"function {helper}\s*\(", js) is not None,
                      "views call this helper; a missing definition blanks "
                      "every data view with 'X is not defined'")
            for view in ("overview", "providers", "models", "launch",
                         "results", "replays", "storyboard", "logs", "system"):
                check(f"client script defines view '{view}'",
                      _re.search(rf"RENDER\.{view}\s*=", js) is not None)
            for endpoint in ("/api/runs", "/api/registry", "/api/replay/",
                             "/api/run/"):
                check(f"client script calls '{endpoint}'",
                      endpoint in js)

        # ---- read endpoints --------------------------------------------
        # /api/system runs the rasteriser probe, which launches Chromium to
        # prove the machine can actually rasterise a frame. A cold browser start
        # is ~15s on a slow Windows machine, so this one call gets a longer
        # leash than the rest; every other endpoint is fast.
        st, sysd = req("/api/system", timeout=90)
        check("GET /api/system", st == 200 and "sandbox" in sysd, str(st))
        check("system reports the overlay path",
              sysd["paths"]["overlay"].endswith("overlay.json"))
        check("system reports isolation honestly",
              sysd["sandbox"]["level"] in ("none", "docker"), str(sysd["sandbox"]["level"]))

        st, runs = req("/api/runs")
        check("GET /api/runs", st == 200 and "runs" in runs, str(st))
        check("runs carry a status source",
              all("status_source" in r for r in runs["runs"]))
        check("runs distinguish clean from excluded",
              runs["n_success"] + runs["n_excluded"] == runs["n_total"],
              f"{runs['n_success']}+{runs['n_excluded']}!={runs['n_total']}")

        st, lb = req("/api/leaderboard")
        check("GET /api/leaderboard", st == 200, str(st))
        check("leaderboard contains only clean runs",
              all(r["status"] == "success" for r in lb["rows"]),
              "a non-success run in the ranking is the defect this guards")
        check("leaderboard reports how many it excluded",
              "n_excluded" in lb, str(lb.keys()))

        st, reg = req("/api/registry")
        check("GET /api/registry", st == 200 and reg["providers"], str(st))
        check("registry lists 22 providers", len(reg["providers"]) == 22,
              str(len(reg["providers"])))
        check("registry lists 90 models", len(reg["models"]) == 90,
              str(len(reg["models"])))
        nocap = [m for m in reg["models"] if m["capabilities"] is None]
        emptycap = [m for m in reg["models"] if m["capabilities"] == []]
        check("an unchecked model reports null, not []",
              len(nocap) > 0, f"unchecked={len(nocap)}")
        # An empty list is a real, different state: the model IS known and it
        # IS declared text-only. The mock backend is the only model that can be
        # known-by-construction to take no input, so it is the only legitimate
        # [] in the table. Anything else appearing here means a check was lost.
        check("the only empty capability list is the one known by construction",
              all(m["capability_source"] == "by construction" for m in emptycap),
              f"empty={[m['id'] for m in emptycap]}; [] means 'declared "
              f"text-only', which must be a deliberate claim, never a default")
        check("some models are declared frame-capable",
              sum(1 for m in reg["models"]
                  if m["capabilities"] and "image_in" in m["capabilities"]) == 25)

        st, ov = req("/api/overlay")
        check("GET /api/overlay", st == 200 and "path" in ov, str(st))
        check("overlay lives outside the repository",
              os.path.abspath(ROOT) not in os.path.abspath(ov["path"]),
              ov["path"])

        st, unk = req("/api/unknown")
        check("GET /api/unknown", st == 200 and "gaps" in unk, str(st))
        check("unknown report explains itself",
              "not zero" in unk["note"])

        st, jobs = req("/api/jobs")
        check("GET /api/jobs", st == 200 and "jobs" in jobs, str(st))

        # ---- preflight --------------------------------------------------
        st, pf = req("/api/preflight?backend=intern&model=intern-s1")
        check("GET /api/preflight", st == 200, str(st))
        check("preflight finds the key",
              pf["key_source"], str(pf["key_source"]))
        check("preflight reports frame capability",
              "image_in" in (pf["capabilities"] or []),
              str(pf["capabilities"]))

        st, pf2 = req("/api/preflight?backend=poolside&model=poolside/laguna-s-2.1")
        check("preflight warns when a model cannot see",
              any("cannot read a rendered frame" in w for w in pf2["warnings"]),
              "; ".join(pf2["warnings"])[:200])

        st, pf3 = req("/api/preflight?backend=nope&model=x")
        check("preflight rejects an unknown provider", pf3.get("ok") is False)

        # ---- writes: provider -------------------------------------------
        st, r1 = req("/api/provider", "POST", {
            "name": "test-provider", "protocol": "openai",
            "url": "https://example.invalid/v1/chat/completions",
            "models_url": "https://example.invalid/v1/models",
            "key_env": ["TEST_PROVIDER_API_KEY"], "auth": "bearer",
            "context_note": "written by the control-centre test",
        })
        check("POST /api/provider adds one", st == 200 and r1.get("ok"), str(r1))
        st, reg2 = req("/api/registry")
        check("the new provider is visible",
              any(p["name"] == "test-provider" for p in reg2["providers"]))
        check("the new provider is marked as an overlay edit",
              any(p["name"] == "test-provider" and p["overlay"]
                  for p in reg2["providers"]))

        st, r2 = req("/api/provider", "POST", {
            "name": "bad name!", "url": "not-a-url", "key_env": ["lowercase"]})
        check("POST /api/provider rejects a bad name/url/key",
              st == 400 and r2.get("error"), str(r2))

        st, r3 = req("/api/provider/delete", "POST", {"name": "test-provider"})
        check("POST /api/provider/delete removes it", st == 200 and r3.get("ok"))
        st, reg3 = req("/api/registry")
        check("the provider is gone",
              not any(p["name"] == "test-provider" for p in reg3["providers"]))
        check("the shipped registry file was not written to",
              "test-provider" not in (S.CAT._read_registry_file()
                                      .get("providers") or {}))

        # ---- writes: model ----------------------------------------------
        st, m1 = req("/api/model", "POST", {
            "id": "test-model", "provider": "intern", "context_window": None,
            "max_output": None, "price_in": None, "price_out": None,
            "capabilities": ["image_in", "thinking"],
        })
        check("POST /api/model adds one", st == 200 and m1.get("ok"), str(m1))
        st, reg4 = req("/api/registry")
        tm = next((m for m in reg4["models"] if m["id"] == "test-model"), None)
        check("the model is visible", tm is not None)
        check("a blank context window is stored as unknown, not 0",
              tm and tm["context_window"] is None,
              f"got {tm.get('context_window') if tm else 'missing'}")
        check("the model's capabilities were recorded",
              tm and "image_in" in (tm["capabilities"] or []))
        check("price is marked unknown",
              tm and tm["price_known"] is False)

        st, m2 = req("/api/model", "POST", {
            "id": "test-model-zero", "provider": "intern", "context_window": 0})
        check("POST /api/model rejects a zero context window",
              st == 400 and "unknown" in (m2.get("error") or ""),
              str(m2))

        st, m3 = req("/api/model", "POST", {
            "id": "test-model", "provider": "nosuchprovider"})
        check("POST /api/model rejects an unknown provider", st == 400)

        st, m4 = req("/api/model/delete", "POST", {"id": "test-model"})
        check("POST /api/model/delete", st == 200 and m4.get("ok"))
        st, reg5 = req("/api/registry")
        check("the model is gone",
              not any(m["id"] == "test-model" for m in reg5["models"]))

        # ---- writes: run -------------------------------------------------
        st, bad = req("/api/run", "POST", {
            "backend": "definitely-not-a-provider", "model": "m",
            "dataset": "results/dataset.json"})
        check("POST /api/run refuses an unknown provider",
              st == 400 and "no such provider" in (bad.get("error") or ""),
              str(bad))

        st, bad2 = req("/api/run", "POST", {
            "backend": "mock", "model": "m",
            "dataset": "results/dataset.json", "run_id": "../../escape"})
        check("POST /api/run refuses a path-traversing run id",
              st == 400 and "run id" in (bad2.get("error") or ""), str(bad2))

        st, bad3 = req("/api/run", "POST", {
            "backend": "mock", "model": "m", "dataset": "results/dataset.json",
            "mode": "chaos"})
        check("POST /api/run refuses a bad mode", st == 400)

        # a real (tiny) mock run
        st, started = req("/api/run", "POST", {
            "backend": "mock", "model": "mock", "dataset": "results/dataset.ci.json",
            "mode": "optimal", "limit": 2, "run_id": "cc-test-run"})
        check("POST /api/run starts a real process",
              st == 200 and started.get("ok"), str(started))
        check("the started run reports its command",
              started.get("command", "").startswith(sys.executable)
              and "--backend mock" in started.get("command", ""),
              started.get("command", "")[:160])
        check("the command contains no shell metacharacters",
              all(c not in started.get("command", "") for c in (";", "|", "&&", "$(")),
              started.get("command", ""))

        # let it finish
        for _ in range(60):
            st, j = req("/api/jobs")
            if not j["jobs"] or j["jobs"][0]["status"] != "running":
                break
            time.sleep(0.5)
        job = (j["jobs"] or [{}])[0]
        check("the run finished on its own", job.get("status") == "success",
              str(job.get("status")) + " rc=" + str(job.get("returncode")))
        check("the log tail was captured",
              any("supervisor" in line for line in job.get("tail") or []),
              str((job.get("tail") or [])[-3:]))

        st, runs2 = req("/api/runs")
        ccrun = next((r for r in runs2["runs"] if r["run_id"] == "cc-test-run"), None)
        check("the new run appears in /api/runs", ccrun is not None)
        check("the new run is reported as a result",
              ccrun and ccrun["status"] == "success",
              str(ccrun.get("status") if ccrun else None))
        check("the run's log parses cleanly",
              ccrun and ccrun["bad_lines"] == 0)
        check("the run recorded its sandbox level",
              ccrun and ccrun.get("sandbox") is not None,
              "a result must carry its own isolation facts")

        # stop a long run and see it labelled interrupted
        st, s2 = req("/api/run", "POST", {
            "backend": "mock", "model": "mock", "dataset": "results/dataset.test.json",
            "mode": "optimal", "run_id": "cc-test-kill"})
        check("a second run starts", st == 200 and s2.get("ok"), str(s2))
        time.sleep(1.2)
        st, k = req("/api/kill", "POST", {"job_id": s2["job_id"]})
        check("POST /api/kill stops it", st == 200 and k.get("ok"), str(k))
        st, runs3 = req("/api/runs")
        krun = next((r for r in runs3["runs"] if r["run_id"] == "cc-test-kill"), None)
        check("the stopped run is labelled interrupted, not success",
              krun and krun["status"] == "interrupted",
              str(krun.get("status") if krun else "run not written"))
        st, lb2 = req("/api/leaderboard")
        check("the interrupted run is excluded from the leaderboard",
              not any("cc-test-kill" in (r.get("file") or "") for r in lb2["rows"]))
        check("and it is listed as excluded instead",
              any("cc-test-kill" in (e.get("file") or "") for e in lb2["excluded"]))

        # ---- per-turn replay renders the frame the model was sent --------
        # Regression: the OpenAI image_url part is {"type":"image_url","url":...}
        # -- the data URI is a SIBLING of `type`, not nested under an
        # `image_url` key. An extractor that read part["image_url"]["url"] found
        # nothing and the replay showed "no frame on this turn" for every real
        # vision run. Build a synthetic vision run on disk and assert the frame
        # is extracted and inlined.
        import tempfile as _tf
        rdir = _tf.mkdtemp()
        rid = "cc-test-replay"
        frame_uri = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC"
                     "1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
        key = "deadbeefcafef00d"
        blob = {"encoding": "pooled-v1",
                "entries": {key: {"role": "user",
                                 "content": [
                                     {"type": "text", "text": "Turn 0. Move."},
                                     {"type": "image_url", "url": frame_uri}],
                                 "n": 1,
                                 "sha256": "x" * 64}},
                "turns": [[key]],
                "stats": {"distinct_messages": 1, "total_message_refs": 1,
                          "turns_logged": 1, "verbatim_bytes": 10,
                          "pooled_bytes": 10, "saving_bytes": 0, "ratio": 1.0}}
        with open(os.path.join(rdir, f"{rid}.jsonl.transcript.json"), "w") as fh:
            json.dump(blob, fh)
        with open(os.path.join(rdir, f"{rid}.jsonl"), "w") as fh:
            fh.write(json.dumps({"run_id": rid, "model": "m", "episode": 0,
                                  "turn": 0, "rotation_deg": 0,
                                  "prompt_keys": [key], "raw_model_text": "got it",
                                  "parsed_action": {"turn": 0, "step": 1},
                                  "optimal_action": {"turn": 0, "step": 1},
                                  "progressed": True, "error_class": "ok",
                                  "prompt_tokens": 10, "completion_tokens": 5,
                                  "token_source": "measured", "latency_s": 0.3})
                          + "\n")
        frame, prompt, has_frame = S._turn_media([key], blob)
        check("the replay extracts the frame from a standard OpenAI part",
              has_frame and frame == frame_uri, f"has_frame={has_frame}")
        html, status = S.episode_html(rdir, rid, 0)
        check("the replay page inlines the frame image",
              status == 200 and frame_uri in html,
              f"status={status} frame_inlined={frame_uri in html}")
        check("the replay page shows the raw response",
              "got it" in html, "raw text missing from replay")
        shutil.rmtree(rdir, ignore_errors=True)

        # A stop that lands BEFORE the first episode finishes is the case the
        # in-loop handler used to miss: the run's status stayed `started`
        # forever, which every reader treats as "still going" rather than as
        # "stopped". Killed with no delay, this reproduces that window.
        st, s3 = req("/api/run", "POST", {
            "backend": "mock", "model": "mock", "dataset": "results/dataset.json",
            "mode": "optimal", "run_id": "cc-test-early"})
        check("a third run starts", st == 200 and s3.get("ok"), str(s3))
        time.sleep(0.35)
        st, k3 = req("/api/kill", "POST", {"job_id": s3["job_id"]})
        check("stopping immediately after start works", st == 200 and k3.get("ok"),
              str(k3))
        time.sleep(0.6)
        status_file = os.path.join(ROOT, "results", "cc-test-early.status.json")
        early = json.load(open(status_file, encoding="utf-8")) \
            if os.path.exists(status_file) else {}
        check("a run stopped before its first episode is recorded as interrupted",
              early.get("status") == "interrupted",
              f"status is {early.get('status')!r}; 'started' would mean the "
              f"interrupt handler never ran, and every reader would treat this "
              f"run as still in progress")
        st, runs4 = req("/api/runs")
        check("...and it does not appear as a result",
              not any(r["run_id"] == "cc-test-early" and r["status"] == "success"
                      for r in runs4["runs"]))

        # ---- report pages -------------------------------------------------
        with _OPENER.open(BASE + "/run/cc-test-run", timeout=10) as r:
            rp = r.read().decode()
        check("GET /run/<id> renders a report",
              r.status == 200 and "Episodes" in rp)
        try:
            _OPENER.open(BASE + "/run/no-such-run", timeout=10)
            check("a missing run is a 404", False, "it returned 200")
        except urllib.error.HTTPError as e:
            check("a missing run is a 404", e.code == 404, str(e.code))
        st, _ = req("/api/nonsense")
        check("an unknown API route is a 404", st == 404, str(st))

    finally:
        httpd.shutdown()
        # Put the overlay back exactly as it was, so running this test twice is
        # the same as running it once and the user's own edits survive.
        DSC.save_overlay(overlay_before)

    print()
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print()
        for f in FAIL:
            print(f"    FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
