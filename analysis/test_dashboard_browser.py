"""Load every dashboard tab in a real browser and fail on a broken render.

Why this exists next to `check_dashboard_js.py`: `node --check` proves the
emitted script *parses*. It cannot prove a view *runs*. Two bugs shipped past
it -- a `const` read above its own declaration (a temporal-dead-zone
ReferenceError) and an unescaped newline in a string -- and both left the page
looking fine until you clicked the tab. The only thing that catches a view that
throws when it renders is a browser that renders it.

The assertions are deliberately strict: a view counts as rendered only if it
produced real content AND did not leave the shared error note behind. An earlier
check that just looked for "some HTML" accepted that error note and hid the
broken Results tab for exactly that reason.

Run:  python analysis/test_dashboard_browser.py
Skips (exit 0) when Playwright is not installed, so it can sit in a suite that
runs on machines without a browser.
"""
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

# Throwaway overlay and settings, set BEFORE the drawtle imports read them. This
# suite drives the Settings tab, which writes settings -- so without this it
# would edit the user's real preferences. A test must not be able to change
# someone's configuration, however convenient that is to write.
_TMP_CFG = tempfile.mkdtemp(prefix="drawtle-browser-cfg-")
os.environ["DRAWTLE_OVERLAY_FILE"] = os.path.join(_TMP_CFG, "overlay.json")
os.environ["DRAWTLE_SETTINGS_FILE"] = os.path.join(_TMP_CFG, "settings.json")

PORT = 8491
BASE = f"http://127.0.0.1:{PORT}"
TABS = ("overview", "live", "providers", "models", "launch", "results",
        "replays", "storyboard", "analytics", "integrity", "logs",
        "system", "settings")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"\n         {detail}" if not cond and detail else ""))


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  SKIP  playwright is not installed; browser smoke test not run")
        return 0

    from web import server as S

    # Generate the manifests the Results view reads, so it renders real rows.
    if not os.path.exists(os.path.join(ROOT, "results", "dataset.json")):
        import subprocess
        subprocess.run([sys.executable, os.path.join(ROOT, "bench.py"),
                        "generate", "--count", "20", "--sizes", "9",
                        "--seed", "7", "--out", "results/dataset.json"],
                       cwd=ROOT, capture_output=True)

    httpd, _sup = S.make_server("results", "127.0.0.1", PORT)
    # Mirror `serve()`: the health check runs at startup and seeds the cached
    # machine facts (docker, rasteriser). Skipping it left the first
    # `/api/system` to pay that cost cold -- up to eight seconds of docker probe
    # plus a real rasterisation -- which made this test flaky on a loaded
    # machine and, worse, made it unrepresentative of what a user sees.
    S._startup_health("results")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.6)

    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            pg = br.new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append(
                "pageerror: " + (getattr(e, "stack", None) or str(e))))
            pg.on("console", lambda m: errors.append(f"console.error: {m.text}")
                  if m.type == "error" else None)

            pg.goto(BASE + "/", wait_until="domcontentloaded")

            def view_body():
                return pg.evaluate(
                    "() => { const v = document.querySelector('#view');"
                    " return v ? v.innerHTML : ''; }")

            for tab in TABS:
                if tab != "overview":
                    pg.click(f'#side nav button[data-view="{tab}"]')
                try:
                    pg.wait_for_function(
                        "() => { const v = document.querySelector('#view');"
                        " return v && !v.querySelector('.spin')"
                        " && v.innerHTML.length > 200; }",
                        # Longer than the page's own 30s render watchdog so a
                        # view that genuinely cannot settle fails with the
                        # watchdog's error note (a real diagnosis) instead of
                        # an ambiguous "loading" -- and a healthy view that is
                        # merely slow on a loaded machine gets a fair window.
                        timeout=32000)
                    # A view paints its shell, then awaits. Give the awaited
                    # part a moment to land (or to throw) before judging it,
                    # otherwise a view that fails after painting looks fine.
                    pg.wait_for_timeout(700)
                    body = view_body()
                    ok = "Could not render this view" not in body
                    detail = body[:240] if not ok else ""
                except Exception as e:                      # noqa: BLE001
                    ok, detail = False, f"never left the spinner ({type(e).__name__})"
                check(f"the {tab} tab renders", ok, detail)

            check("no javascript error was raised on any tab", not errors,
                  "; ".join(errors[:5]))

            # This release adds the dataset filter to the Overview leaderboard
            # (runs are comparable only within one dataset) and the live window
            # (a real-time turn stream). Both must render in a real browser.
            pg.click('#side nav button[data-view="overview"]')
            try:
                pg.wait_for_selector("#lb-ds", timeout=15000)
                ds_ok = True
            except Exception:                                  # noqa: BLE001
                ds_ok = False
            check("the Overview leaderboard offers a dataset filter",
                  ds_ok, "no #lb-ds control appeared")
            pg.click('#side nav button[data-view="live"]')
            pg.wait_for_timeout(600)
            live_txt = pg.inner_text("#view")
            check("the Live window view renders",
                  "Live window" in live_txt, live_txt[:140])

            # M4: the System view shows the API-keys panel with a save control
            # per provider. The tab already rendered in the loop above; this
            # pins that the keys surface is actually present, not just that the
            # System tab did not throw.
            pg.click('#side nav button[data-view="system"]')
            pg.wait_for_selector("#view")
            pg.wait_for_timeout(300)
            sys_txt = pg.inner_text("#view")
            check("the System view shows the API keys panel",
                  "API keys" in sys_txt, sys_txt[:120])
            n_key_inputs = pg.eval_on_selector_all(
                "#view button[data-key-save]", "els => els.length")
            check("the API keys panel offers a save control per provider",
                  n_key_inputs > 0, f"inputs={n_key_inputs}")

            # ---- M3: Docker control surface --------------------------------
            # The panel must be present, and when Docker is not installed (this
            # machine) it must show its state and NOT offer actions -- offering a
            # Build button that can only fail would be false comfort. A real
            # host with Docker gets the five buttons instead.
            check("the System view shows the Docker control panel",
                  "Docker control" in sys_txt, sys_txt[:200])
            # The action buttons are all-or-nothing (docker present or
            # withheld) -- never partial, never runnable when the daemon is
            # down. The Refresh control is always there: it is how the user
            # re-probes after starting Docker Desktop without reloading.
            # Five, not six: there is no "Start all". `compose up -d` starts
            # the bench one-shot too, and its command is a full mock benchmark
            # that would run into the operator's live results the moment they
            # brought Docker up. A benchmark is an explicit act (Smoke-test),
            # never a side effect of starting an environment.
            n_dk_actions = pg.eval_on_selector_all(
                "#view button[data-act='dk-build'], "
                "#view button[data-act='dk-smoke'], "
                "#view button[data-act='dk-proxy-on'], "
                "#view button[data-act='dk-proxy-off'], "
                "#view button[data-act='dk-down']", "els => els.length")
            check("Docker actions are all-or-nothing (present or withheld)",
                  n_dk_actions in (0, 5), f"dk action buttons={n_dk_actions}")
            # The panel body fills ASYNCHRONOUSLY after the view paints (the
            # docker probe can take a couple of seconds), so wait for the
            # Refresh control rather than sampling the DOM early.
            try:
                pg.wait_for_selector(
                    "#view button[data-act='dk-refresh']", timeout=25000)
                refresh_ok = True
            except Exception:                                  # noqa: BLE001
                refresh_ok = False
            check("the Docker panel always offers a live refresh",
                  refresh_ok, "no dk-refresh control appeared")
            pg.click('#side nav button[data-view="launch"]')
            pg.wait_for_selector("#l-sandbox", timeout=8000)
            check("the Launch form offers the sandbox container option",
                  pg.evaluate("() => !!document.querySelector('#l-sandbox')"),
                  "no sandbox checkbox on the launch form")


            # Launch: choosing a provider must narrow the model list to that
            # provider's models. Asserted rather than assumed -- the whole point
            # of the control is that a run cannot be aimed at the wrong endpoint.
            pg.click('#side nav button[data-view="launch"]')
            pg.wait_for_selector("#l-backend")
            total = pg.eval_on_selector_all("#l-model-list option", "els => els.length")
            narrowed = pg.evaluate("""() => {
              const sel = document.querySelector('#l-backend');
              for (const o of Array.from(sel.options)) {
                sel.value = o.value;
                sel.dispatchEvent(new Event('change'));
                const n = document.querySelectorAll('#l-model-list option').length;
                if (n > 0 && n < 90) return {provider: o.value, n: n};
              }
              return null;
            }""")
            check("the model list is provider-filtered",
                  bool(narrowed) and narrowed["n"] < total,
                  f"all providers={total}, best filter={narrowed}")

            # Every launch field must carry a tooltip.
            tips = pg.evaluate(
                "() => document.querySelectorAll('#view label.f .q').length")
            fields = pg.evaluate(
                "() => document.querySelectorAll('#view label.f').length")
            check("every launch field has a tooltip",
                  fields > 0 and tips == fields, f"{tips} tooltips / {fields} fields")

            # Tooltips must be INSTANT. The client replaces the OS title (which
            # waits ~500ms) with one #tip element shown on hover, and strips the
            # native title so the slow one can never also fire. A 120ms sample
            # is far inside the native delay, so an "on" tooltip at that point
            # proves the custom path, not the browser's.
            pg.hover("#l-sandbox-hint")
            pg.wait_for_timeout(140)
            tip_state = pg.evaluate(
                "() => { const t = document.querySelector('#tip');"
                " return t ? {on: t.className.indexOf('on') >= 0,"
                "             text: (t.textContent || '').slice(0, 80)} : null; }")
            check("hovering shows the tooltip instantly",
                  bool(tip_state and tip_state["on"] and tip_state["text"]),
                  str(tip_state))
            check("the native title was replaced, not duplicated",
                  pg.evaluate("() => !document.querySelector('#l-sandbox-hint')"
                              ".hasAttribute('title')"),
                  "the OS title is still attached; a delayed native tooltip "
                  "will appear beside the instant one")

            # ONE click must raise ONE confirmation dialog, every time.
            #
            # A view that re-renders itself calls RENDER.x(v) on the SAME
            # element, so a bare addEventListener stacks another handler each
            # time and the Nth click fires N dialogs. The user had to dismiss
            # the same dialog five or six times, and because the first handler
            # deleted the row while the rest ran against it afterwards, the
            # delete looked like it had failed.
            #
            # The dialog is ACCEPTED, not dismissed: dismissing it makes the
            # handler return early, so nothing re-renders and no handler ever
            # stacks -- a version of this test that dismissed passed happily
            # with the bug in place. The providers live in a throwaway overlay
            # (see the top of this file), so deleting a few is safe.
            dialogs = []
            pg.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
            pg.click('#side nav button[data-view="providers"]')
            pg.wait_for_selector("#view table")
            per_click = []
            for _ in range(3):
                before = len(dialogs)
                btn = pg.query_selector('#view button[data-act="del-provider"]')
                if not btn:
                    break
                btn.click()
                pg.wait_for_timeout(900)   # let the POST land and the view redraw
                per_click.append(len(dialogs) - before)
            check("each delete click raises exactly one confirmation",
                  len(per_click) >= 2 and all(n == 1 for n in per_click),
                  f"dialogs per click: {per_click} (a rising count means "
                  f"handlers are stacking on the same element)")
            # ---- Results: filters, sorting, and a retention PREVIEW --------
            # Switch to TEST mode first. The committed mock runs are self-tests,
            # so live mode correctly hides them and every table would be empty --
            # which is the feature working, not a reason to weaken the test.
            pg.click("#tg-test")
            pg.wait_for_timeout(900)
            check("the test-mode toggle is reflected in the header",
                  pg.evaluate("() => document.querySelector('#tg-test')"
                              ".getAttribute('aria-pressed')") == "true",
                  "aria-pressed did not become true")
            check("test mode shows its warning banner",
                  pg.evaluate("() => !!document.querySelector('#banner .testbanner')"),
                  "no banner: the page could be showing self-test data while "
                  "looking like a normal view")

            pg.click('#side nav button[data-view="results"]')
            pg.wait_for_selector("#res-q")
            rows_all = pg.eval_on_selector_all("#res-bad table tbody tr",
                                               "els => els.length")
            check("the results table renders rows to filter",
                  rows_all >= 1, f"{rows_all} excluded row(s)")
            pg.fill("#res-q", "mock-opt")
            pg.wait_for_timeout(300)
            rows_one = pg.eval_on_selector_all("#res-bad table tbody tr",
                                               "els => els.length")
            check("the search filter narrows the table",
                  rows_one < rows_all and rows_one >= 1,
                  f"{rows_all} -> {rows_one}")
            pg.fill("#res-q", "")
            pg.wait_for_timeout(250)
            rows_back = pg.eval_on_selector_all("#res-bad table tbody tr",
                                                "els => els.length")
            check("clearing the filter restores every row",
                  rows_back == rows_all, f"{rows_back} vs {rows_all}")

            # Sort via the EXCLUDED table's header. The clean table renders an
            # empty state here (results/ holds no clean runs), so it has no
            # headers at all -- targeting `#view th` would find nothing.
            pg.click('#res-bad th[data-sort="run_id"]')
            pg.wait_for_timeout(250)
            ind = pg.evaluate(
                "() => Array.from(document.querySelectorAll('#res-bad .sortind'))"
                ".map(e => e.textContent).join('')")
            check("clicking a header marks that column as sorted",
                  bool(ind.strip()), f"indicators: {ind!r}")

            # The retention preview runs a DRY RUN on the server. It must either
            # offer a confirm step, or explain why nothing matched -- and in
            # neither case may it have deleted anything. A sweep that removes
            # the wrong runs is unrecoverable, so the preview exists precisely so
            # the plan is seen before it is executed.
            #
            # In this fixture every test run is a committed reference artifact,
            # so the correct outcome is "nothing to delete, and here is why".
            # Both outcomes are accepted; deleting is not.
            pg.click('#view button[data-act="prune-preview"][data-prune="test"]')
            pg.wait_for_timeout(2500)
            state = pg.evaluate("""() => {
              const out = document.querySelector('#prune-out');
              if (!out) return {out: null};
              return {hasGo: !!out.querySelector('button[data-act="prune-go"]'),
                      text: (out.textContent || '').slice(0, 220)};
            }""")
            check("the retention preview offers a confirm step or explains why not",
                  state["hasGo"] or "Nothing would be deleted" in (state["text"] or ""),
                  f"preview output: {state}")
            check("the preview deleted nothing",
                  all(os.path.exists(os.path.join(ROOT, "results", f))
                      for f in ("mock-opt.jsonl", "mock-opt.summary.json",
                                "mock-stale.jsonl", "mock-stale.summary.json")),
                  "a committed reference artifact is gone -- the preview "
                  "executed instead of planning")

            # ---- M5: the lightbox opens on a frame click and closes on Esc ----
            # The replay filmstrip, the per-turn frame, and the storyboard
            # thumbnails all open the SAME lightbox through one document-level
            # delegated handler. No real run in this fixture carries a frame, so
            # we exercise the shared handler with a synthetic .strip image and
            # assert the overlay appears and dismisses.
            opened = pg.evaluate("""() => {
              const img = document.createElement('img');
              img.className = 'strip';
              img.src = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';
              document.body.appendChild(img);
              img.click();
              return !!document.querySelector('.lb-wrap');
            }""")
            check("clicking a frame opens the lightbox", opened,
                  "no .lb-wrap appeared after the click")
            closed = pg.evaluate("""() => {
              document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
              return !document.querySelector('.lb-wrap');
            }""")
            check("Escape closes the lightbox", closed,
                  "the overlay was still present after Esc")

            # ---- M6: analytics + integrity render -----------------------
            # (Test mode is on from the Results section, so these aggregate the
            # committed mock runs rather than an empty live view.) These wait
            # for the spinner to leave, not a fixed sleep: the aggregate reads
            # every run's log, which under CPU contention can take longer than
            # any fixed wait -- and a flaky check is a check nobody trusts.
            pg.click('#side nav button[data-view="analytics"]')
            pg.wait_for_function(
                "() => { const v = document.querySelector('#view');"
                " return v && !v.querySelector('.spin') && v.innerHTML.length > 200; }",
                timeout=25000)
            an_txt = pg.inner_text("#view")
            check("the Analytics view renders with a by-provider table",
                  "Analytics" in an_txt and "By provider" in an_txt, an_txt[:140])
            pg.click('#side nav button[data-view="integrity"]')
            pg.wait_for_function(
                "() => { const v = document.querySelector('#view');"
                " return v && !v.querySelector('.spin') && v.innerHTML.length > 200; }",
                timeout=25000)
            ig_txt = pg.inner_text("#view")
            check("the Integrity view renders",
                  "Integrity" in ig_txt, ig_txt[:140])

            # ---- Vision probe (test-mode-only tab) ----------------------
            # Test mode is ON from the Results section, so the Diagnostics group
            # with the vprobe entry must be visible now. The probe is the one
            # tab that turns a real endpoint call (render-only here: no key, no
            # model call, just prove the environment produces a maze image that
            # the UI can display).
            probe_visible = pg.evaluate(
                "() => { const b = document.querySelector("
                "'#side nav button[data-view=\"vprobe\"]');"
                " return b && b.style.display !== 'none' && "
                "getComputedStyle(b).display !== 'none'; }")
            check("the vision-probe entry is visible in test mode",
                  probe_visible, "entry hidden while test mode is on")
            pg.click('#side nav button[data-view="vprobe"]')
            pg.wait_for_selector("#vp-ask", timeout=8000)
            vp_txt = pg.inner_text("#view")
            check("the Vision probe view renders",
                  "Vision probe" in vp_txt and "Model under test" in vp_txt,
                  vp_txt[:160])
            # Render-only: no model id from the registry is required to be a
            # real vision model for this step, but the form demands a model id,
            # so use the mock.
            pg.fill("#vp-model", "mock")
            pg.click("#vp-preview")
            pg.wait_for_function(
                "() => { const im = document.querySelector('#vp-out .vp-frame img');"
                " return !!im && im.src.startsWith('data:image/png'); }",
                timeout=20000)
            check("render-only shows the maze frame as a data URI",
                  True, "no .vp-frame image appeared")
            vp_txt2 = pg.inner_text("#view")
            check("render-only reports that no model was called",
                  "No model was called" in vp_txt2, vp_txt2[:200])
            # The entry must vanish again when leaving test mode -- live mode
            # must not offer a diagnostic that its server would refuse.
            pg.click("#tg-test")
            pg.wait_for_timeout(900)
            probe_hidden = pg.evaluate(
                "() => { const b = document.querySelector("
                "'#side nav button[data-view=\"vprobe\"]');"
                " return b && (b.style.display === 'none' || "
                "getComputedStyle(b).display === 'none'); }")
            check("the vision-probe entry hides when test mode turns off",
                  probe_hidden, "entry still visible in live mode")
            pg.click("#tg-test")      # back to test mode for any later checks
            pg.wait_for_timeout(900)

            br.close()
    finally:
        httpd.shutdown()

    print()
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"    FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
