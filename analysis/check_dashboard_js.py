"""Guard: the dashboard's client-side JS must parse.

The SPA is a Python f-string that emits JavaScript. It is easy for a stray
quote or an unescaped newline to produce a page the browser silently refuses to
run -- and the failure mode is the worst one: the page loads, the header
renders, the body says "loading" forever, and nothing in the server logs
mentions it. This check renders the real page and parses its <script> with
node --check, so a broken dashboard fails in CI, not in front of a user.
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, ".")


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP  dashboard JS check: node not on PATH")
        return 0
    from web import views
    html = views.page(version="test", state={})
    start = html.index("<script>") + len("<script>")
    js = html[start:html.index("</script>", start)]
    # A system temp file, never inside the repo. A check that drops scratch
    # files into results/ is a check that pollutes the directory it is meant to
    # be validating -- and results/ is the one place where a stray file looks
    # like data.
    fd, tmp = tempfile.mkstemp(prefix="dash-js-check-", suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(js)
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if r.returncode != 0:
        print("FAIL  dashboard JS does not parse:")
        print(r.stderr[:600])
        return 1
    print("PASS  dashboard JS parses cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
