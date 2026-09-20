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
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PORT = 8491
BASE = f"http://127.0.0.1:{PORT}"
TABS = ("overview", "providers", "models", "launch", "results",
        "replays", "storyboard", "logs", "system")

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
