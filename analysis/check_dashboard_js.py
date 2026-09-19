"""Guard: the dashboard's client-side JS must parse.

The SPA is a Python f-string that emits JavaScript. It is easy for a stray
quote or an unescaped newline to produce a page the browser silently refuses to
run -- and the failure mode is the worst one: the page loads, the header
renders, the body says "loading" forever, and nothing in the server logs
mentions it. This check renders the real page and parses its <script> with
node --check, so a broken dashboard fails in CI, not in front of a user.
"""
import shutil
import subprocess
import sys

sys.path.insert(0, ".")

from web import views


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP  dashboard JS check: node not on PATH")
        return 0
    html = views.page(version="test", state={})
    start = html.index("<script>") + len("<script>")
    js = html[html.index("</script>", start):]
    js = html[start:html.index("</script>", start)]
    tmp = "results/_dash_js_check.js"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(js)
    r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
    import os
    os.remove(tmp)
    if r.returncode != 0:
        print("FAIL  dashboard JS does not parse:")
        print(r.stderr[:600])
        return 1
    print("PASS  dashboard JS parses cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
