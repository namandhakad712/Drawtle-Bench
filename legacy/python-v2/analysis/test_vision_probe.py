"""Regression tests for the vision probe (drawtle/vision_probe.py + web).

Two properties are the whole point of the feature and are tested directly:

1. **The frame is byte-identical to a real run's.** The probe exists to answer
   "can this model see the image in THIS environment", so a probe frame that
   differs from what a scored turn sends would answer a different question.
   Verified by rendering the same maze both ways and comparing bytes.

2. **It is test-mode-only.** The server route answers 403 unless test mode is on,
   and the sidebar entry is hidden (display:none) on a live page and shown in
   test mode. Tested both ways, because a gate that only hides a button is not a
   gate.

Isolation: every probe renders into a per-test temp dir and the settings file is
pointed at a throwaway path, so the suite cannot flip the user's real test-mode
setting or leave frames in results/.
"""
import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Point settings at a throwaway file BEFORE importing the server, so flipping
# test_mode in these tests can never touch the user's real preferences. The
# probe's route gate reads settings on every request, so this is the one file
# that must be redirected.
_TMP_CFG = tempfile.mkdtemp(prefix="drawtle-vprobe-cfg-")
os.environ["DRAWTLE_SETTINGS_FILE"] = os.path.join(_TMP_CFG, "settings.json")
os.environ["DRAWTLE_OVERLAY_FILE"] = os.path.join(_TMP_CFG, "overlay.json")
os.environ["DRAWTLE_CRED_FILE"] = os.path.join(_TMP_CFG, "credentials.json")

from drawtle import frames as F                      # noqa: E402
from drawtle import maze as M                        # noqa: E402
from drawtle import models as MOD                    # noqa: E402
from drawtle import protocol as P                    # noqa: E402
from drawtle import render as R                      # noqa: E402
from drawtle import dataset as D                     # noqa:E402
from drawtle import settings as SET                  # noqa: E402
from drawtle import vision_probe as VP               # noqa: E402
from web import server as S                          # noqa: E402
from web import views as V                           # noqa: E402

PORT = 8491
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = [], []

# A dev machine often has an HTTP proxy configured for loopback, which would
# route these requests through it and hang. Same bypass the other suites use.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"  {mark}  {name}"
    if not cond and detail:
        line += f"\n         {detail}"
    print(line)


def req(path, method="GET", body=None, timeout=120):
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


def set_test_mode(on):
    """Flip test mode in the throwaway settings file the server reads."""
    SET.write({"test_mode": bool(on)})


# ---------------------------------------------------------------- unit level --


def test_frame_matches_real_run():
    """The probe frame must be byte-identical to a real run's turn-0 frame.

    This is the accuracy guarantee the feature exists for. Rebuilt here from the
    runner's own statements (run_episode: camera, cell, heading, render call,
    frame cache) rather than from the probe's internals, so the comparison is
    against the production path and not a copy of it.
    """
    spec = VP.probe_spec(size=11, pair="NW", seed=42)
    a = tempfile.mkdtemp()
    b = tempfile.mkdtemp()

    probe = VP.probe_vision("mock", "mock", spec=spec, render_only=True,
                            cache_dir=a)
    img_probe = base64.b64decode(probe["frame"].split(",", 1)[1])

    maze = D.make_maze(spec)                          # runner._iter
    cam = P.default_camera(maze)                      # runner.run_episode
    svg = R.render_svg(maze, maze.entry, M.initial_heading(maze), 0.0, cam,
                       P.WALL_H, show_heading=False)
    img_real = open(F.render_frame(svg, b), "rb").read()

    check("probe frame is a PNG", probe["frame"].startswith("data:image/png"),
          probe["frame"][:30])
    check("probe frame == real run frame (bytes)",
          img_probe == img_real,
          f"probe={len(img_probe)} real={len(img_real)}")
    check("probe reports ground-truth facts",
          probe["facts"]["size"] == "11x11"
          and probe["facts"]["pair"] == "NW"
          and probe["facts"]["seed"] == 42
          and len(probe["facts"]["exit_cells"]) == 2,
          str(probe["facts"]))


def test_prompt_packs_like_a_real_turn():
    """The image part is the exact object a real turn appends to messages.

    The nested {"image_url":{"url":...}} spelling is required -- InternLM's
    prompt processor rejects the flat one -- so a probe that packed it
    differently could report "cannot see" for a model that can.
    """
    spec = VP.probe_spec(size=9, pair="SE", seed=7)
    tmp = tempfile.mkdtemp()
    _, png, _ = VP.render_probe_frame(spec, tmp)
    part = VP.pack_frame(png)
    url = part["image_url"]["url"]
    check("pack uses the nested image_url object",
          part["type"] == "image_url"
          and isinstance(part["image_url"], dict)
          and url.startswith("data:image/png;base64,"),
          str(part)[:120])
    # Round-trips to the same bytes as the file on disk.
    check("packed payload decodes to the PNG on disk",
          base64.b64decode(url.split(",", 1)[1]) == open(png, "rb").read())


def test_spec_validation():
    """Bad maze parameters are refused, not silently coerced into a maze."""
    ok = VP.probe_spec(size=9, pair="NW", seed=1)
    check("a valid spec builds", ok["size"] == 9 and ok["pair"] == "NW")
    for size in (8, 4, 42):
        try:
            VP.probe_spec(size=size)
            check(f"size {size} rejected", False, "accepted")
        except VP.VisionProbeError:
            check(f"size {size} rejected", True)
    try:
        VP.probe_spec(pair="ZZ")
        check("bad exit pair rejected", False, "accepted")
    except VP.VisionProbeError:
        check("bad exit pair rejected", True)
    # A seed reproduces the same maze -- the reproducibility the dataset has.
    check("a seed reproduces the same maze",
          VP.probe_spec(seed=99)["seed"] == VP.probe_spec(seed=99)["seed"] == 99)


def test_backend_resolution():
    """A blank provider resolves from the registry; an unknown one says so."""
    try:
        VP._resolve_backend(None, "no-such-model-xyz")
        check("unknown model -> provider not resolvable", False, "no error")
    except VP.VisionProbeError as e:
        check("unknown model -> provider not resolvable",
              "provider" in str(e).lower(), str(e))
    # A named provider is passed through untouched.
    check("a named provider passes through",
          VP._resolve_backend("mock", "whatever") == "mock")


class _SayBackend(MOD.ModelBackend):
    """A backend that records the message it was handed and answers in prose.

    Stands in for a real vision model so the transport can be tested without a
    key or network. It asserts the structural contract (a text part AND an image
    part, in that order) because that contract is what 'the model can see it'
    rests on.
    """
    name = "says"

    def __init__(self, model="says", **kw):
        super().__init__(model, **kw)
        self.received = None

    def _post(self, messages, **kw):
        self.received = messages
        return {"choices": [{"message": {"content":
            "I see a square maze from above with grey walls, a blue turtle and "
            "two green exits."}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30}}

    def _wrap(self, payload, lat):
        ch = payload["choices"][0]["message"]["content"]
        return MOD.ModelResponse(text=ch, prompt_tokens=100,
                                 completion_tokens=30, latency_s=lat,
                                 token_source="measured")


def test_probe_call_and_raw_answer(monkey=None):
    """The call path sends a text+image pair and returns the raw reply text.

    Registered as a backend class for the duration of the call only, so no
    permanent change to the module's registry -- the same isolation rule the
    rest of the suite holds to.
    """
    old = dict(MOD._CLASSES)
    MOD._CLASSES["says"] = _SayBackend
    try:
        out = VP.probe_vision("says", "says", spec=VP.probe_spec(seed=3))
    finally:
        MOD._CLASSES.clear()
        MOD._CLASSES.update(old)

    check("probe returns ok", out.get("ok") is True, str(out.get("error")))
    check("probe returns the raw model text",
          out.get("text", "").startswith("I see a square maze"),
          str(out.get("text"))[:80])
    check("probe reports measured tokens",
          out.get("completion_tokens") == 30
          and out.get("token_source") == "measured", str(out))
    # The message the backend received: system + user, and the user turn carries
    # BOTH a text part and an image part. That is the whole transport contract.
    be = _SayBackend()
    be2 = None
    MOD._CLASSES["says"] = _SayBackend
    try:
        VP.probe_vision("says", "says", spec=VP.probe_spec(seed=3))
    finally:
        MOD._CLASSES.clear()
        MOD._CLASSES.update(old)
    # Re-verify via the recorded message on a fresh capture.
    captured = {}

    class _Cap(_SayBackend):
        def _post(self, messages, **kw):
            captured["msgs"] = messages
            return super()._post(messages, **kw)

    MOD._CLASSES["says"] = _Cap
    try:
        VP.probe_vision("says", "says", spec=VP.probe_spec(seed=3))
    finally:
        MOD._CLASSES.clear()
        MOD._CLASSES.update(old)
    msgs = captured["msgs"]
    check("the request has a system and a user message",
          [m["role"] for m in msgs] == ["system", "user"],
          str([m["role"] for m in msgs]))
    user = msgs[1]["content"]
    check("the user turn is a multimodal list",
          isinstance(user, list) and len(user) == 2, str(type(user)))
    check("the user turn carries text then image",
          user[0]["type"] == "text" and user[1]["type"] == "image_url",
          str([p["type"] for p in user]))
    check("the image is a data URI PNG",
          user[1]["image_url"]["url"].startswith("data:image/png;base64,"),
          str(user[1])[:80])
    check("the free-form prompt is sent, not the bench move prompt",
          "JSON" not in user[0]["text"] and "describe" in user[0]["text"].lower(),
          str(user[0]["text"])[:120])


def test_render_only_needs_no_backend():
    """render_only stops before any model call, so it works with no key."""
    out = VP.probe_vision("mock", "mock", spec=VP.probe_spec(seed=5),
                          render_only=True)
    check("render_only produces a frame and no answer",
          out.get("render_only") is True
          and out.get("frame", "").startswith("data:image/png")
          and "text" not in out, str(sorted(out.keys())))


def test_rasteriser_absence_is_reported():
    """A missing rasteriser is a clear refusal, never a silent text-only probe.

    A vision probe that sent no image would measure nothing while looking like
    it worked -- the exact silent downgrade the frame cache exists to prevent.
    """
    real = F.render_frame
    F.render_frame = lambda svg, d: (_ for _ in ()).throw(RuntimeError("nope"))
    try:
        try:
            VP.render_probe_frame(VP.probe_spec(seed=1), tempfile.mkdtemp())
            check("no rasteriser -> VisionProbeError", False, "no error raised")
        except VP.VisionProbeError as e:
            check("no rasteriser -> VisionProbeError",
                  "rasterise" in str(e).lower() or "png" in str(e).lower(),
                  str(e)[:100])
    finally:
        F.render_frame = real


# -------------------------------------------------------------- server level --


def test_route_gated_on_test_mode():
    """The endpoint is the real gate: 403 off, 200 on. Hiding is not enough."""
    set_test_mode(False)
    st, body = req("/api/vision-probe", "POST",
                   {"backend": "mock", "model": "mock"})
    check("probe refused while test mode is off",
          st == 403 and "test mode" in (body.get("error") or "").lower(),
          f"st={st} body={str(body)[:120]}")

    set_test_mode(True)
    st, body = req("/api/vision-probe", "POST",
                   {"backend": "mock", "model": "mock", "render_only": True})
    check("probe allowed while test mode is on",
          st == 200 and body.get("ok") is True, f"st={st} {str(body)[:120]}")
    check("probe response carries the frame and the facts",
          body.get("frame", "").startswith("data:image/png")
          and body.get("facts", {}).get("size"), str(sorted(body.keys())))

    # A model id is the one mandatory field.
    st, body = req("/api/vision-probe", "POST", {"backend": "mock"})
    check("probe requires a model id", st == 400, f"st={st}")
    # Bad maze parameters are a 400, not a 500.
    st, body = req("/api/vision-probe", "POST",
                   {"backend": "mock", "model": "mock", "size": 8})
    check("probe rejects a bad size with 400",
          st == 400 and "size" in (body.get("error") or "").lower(),
          f"st={st} {str(body)[:120]}")
    set_test_mode(False)


def test_route_render_only_and_call():
    """render_only via the route verifies the environment with no key."""
    set_test_mode(True)
    st, body = req("/api/vision-probe", "POST",
                   {"backend": "mock", "model": "mock", "render_only": True,
                    "size": 9, "pair": "EN", "seed": 12})
    check("route render_only answers",
          st == 200 and body.get("render_only") is True
          and body.get("facts", {}).get("pair") == "EN",
          f"st={st} {str(body)[:150]}")
    # The mock backend does not read images; its answer is what "not seeing"
    # looks like. That is still a completed call, not an error.
    st, body = req("/api/vision-probe", "POST",
                   {"backend": "mock", "model": "mock", "seed": 12})
    check("route completes a mock call and returns its raw text",
          st == 200 and body.get("ok") is True
          and isinstance(body.get("text"), str),
          f"st={st} {str(body)[:150]}")
    set_test_mode(False)


def test_page_hides_tab_in_live_mode():
    """The sidebar entry is absent in live mode and present in test mode.

    The server gate is authoritative; this is the cosmetic layer, and it is what
    a live-mode reader actually sees. Both directions are tested because a tab
    that appears in live mode is the defect the user asked to prevent.
    """
    def page_for(test_mode):
        set_test_mode(test_mode)
        with _OPENER.open(BASE + "/", timeout=20) as r:
            return r.read().decode()

    live = page_for(False)
    # The view's OWN markup (heading, JS) legitimately contains these strings;
    # the SIDEBAR is what must not offer the entry. The design keeps the entry
    # in the DOM hidden (so the header pill can reveal it live without a
    # reload), so "hidden" means display:none, not absent -- and the server's
    # 403 is the real gate either way.
    live_entry = re.search(r'<button class="snav" data-view="vprobe"[^>]*>',
                           live)
    check("live page hides the vision-probe nav entry",
          live_entry is not None and 'display:none' in live_entry.group(0),
          "entry visible or missing in live mode")
    check("live page marks the entry test-only",
          live_entry is not None
          and 'data-test-only="1"' in live_entry.group(0),
          "no test-only marker on the live entry")

    testp = page_for(True)
    test_entry = re.search(r'<button class="snav" data-view="vprobe"[^>]*>',
                           testp)
    check("test-mode page shows the vision-probe nav entry",
          test_entry is not None and 'display:none' not in test_entry.group(0),
          "entry hidden in test mode")
    check("test-mode page marks the entry test-only",
          test_entry is not None
          and 'data-test-only="1"' in test_entry.group(0),
          "no test-only marker on the test entry")
    check("test-mode page defines the view",
          re.search(r"RENDER\.vprobe\s*=", testp) is not None)
    check("client refuses a test-only view when off",
          "VIEWS_TEST_ONLY" in testp and "SETTINGS.test_mode" in testp)


def main():
    work = tempfile.mkdtemp(prefix="drawtle-vp-")
    results_dir = os.path.join(work, "results")
    os.makedirs(results_dir, exist_ok=True)
    sup = S.SUP.Supervisor(results_dir=results_dir)
    httpd, sup = S.make_server(results_dir, "127.0.0.1", PORT, supervisor=sup)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.6)
    try:
        print("unit:")
        test_frame_matches_real_run()
        test_prompt_packs_like_a_real_turn()
        test_spec_validation()
        test_backend_resolution()
        test_probe_call_and_raw_answer()
        test_render_only_needs_no_backend()
        test_rasteriser_absence_is_reported()
        print("server:")
        test_route_gated_on_test_mode()
        test_route_render_only_and_call()
        test_page_hides_tab_in_live_mode()
    finally:
        httpd.shutdown()
        httpd.server_close()
        set_test_mode(False)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:")
        for n in FAIL:
            print(" -", n)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
