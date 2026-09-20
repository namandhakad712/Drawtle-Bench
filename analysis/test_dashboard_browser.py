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
TABS = ("overview", "providers", "models", "launch", "results",
        "replays", "storyboard", "logs", "system", "settings")

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
                    pg.click(f'nav.tabs button[data-view="{tab}"]')
                try:
                    pg.wait_for_function(
                        "() => { const v = document.querySelector('#view');"
                        " return v && !v.querySelector('.spin')"
                        " && v.innerHTML.length > 200; }",
                        timeout=20000)
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

            # Launch: choosing a provider must narrow the model list to that
            # provider's models. Asserted rather than assumed -- the whole point
            # of the control is that a run cannot be aimed at the wrong endpoint.
            pg.click('nav.tabs button[data-view="launch"]')
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
            pg.click('nav.tabs button[data-view="providers"]')
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
