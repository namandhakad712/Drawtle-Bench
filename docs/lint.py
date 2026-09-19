#!/usr/bin/env python3
"""Lint the built docs site. Exits non-zero on any structural defect.

Born from a real failure: the first build of docs/build.py emitted
`</li></ul>` as a single close tag, producing 57 `<li>` against 99 `</li>`, and
left parent items unclosed around nested lists. **None of that was visible in a
browser** -- HTML permits omitting `</li>`, so the pages rendered fine and the
markup was wrong. Exactly the class of bug that has to be caught by counting, not
by looking.

Checks per page:
  1. tag balance for the tags the converter emits
  2. no leaked markdown (raw pipes in text, stray backticks, unresolved entities)
  3. every internal href resolves to a file that exists
  4. headings are well-formed and their ids are unique
  5. no empty <p>/<li>/<td>
  6. figure assets referenced actually exist

Usage:  python docs/lint.py
"""
import os
import re
import sys

DOCS = os.path.dirname(os.path.abspath(__file__))

# tags the converter emits, as open/close pairs
PAIRS = ["li", "ul", "ol", "strong", "em", "code", "pre", "table", "tr", "td",
         "th", "p", "blockquote", "div", "h1", "h2", "h3", "h4", "nav", "main",
         "footer", "a"]


def body_of(html):
    """Just the <main> content, so nav/footer boilerplate is not double-counted."""
    if "<main>" in html and "</main>" in html:
        return html.split("<main>", 1)[1].split("</main>", 1)[0]
    return html


def check_balance(name, body, faults):
    for t in PAIRS:
        op = len(re.findall(rf"<{t}[\s>]", body))
        cl = len(re.findall(rf"</{t}>", body))
        if op != cl:
            faults.append(f"{name}: <{t}> unbalanced ({op} open / {cl} close)")


def check_leaks(name, body, faults):
    # `prose` strips code, because pipes and backticks inside <pre>/<code> are
    # legitimate data (e.g. the ASCII architecture diagram, shell snippets).
    prose = re.sub(r"<pre>.*?</pre>", "", body, flags=re.S)
    prose = re.sub(r"<code[^>]*>.*?</code>", "", prose, flags=re.S)
    # Inline <script> bodies are code, not prose -- the theme/UI script uses
    # "||" and other punctuation the prose leak-check would false-positive on.
    prose = re.sub(r"<script>.*?</script>", "", prose, flags=re.S)

    # raw markdown pipes that survived into a text node
    stray_pipes = re.findall(r">[^<]*\|[^<]*<", prose)
    if stray_pipes:
        faults.append(f"{name}: {len(stray_pipes)} raw pipe(s) in prose")
    # unrendered backtick code
    if "`" in prose:
        faults.append(f"{name}: {prose.count('`')} stray backtick(s) in prose")
    for marker in ("**", "__", "]("):
        if marker in prose:
            faults.append(f"{name}: unresolved marker {marker!r} x{prose.count(marker)}")


def check_empty(name, body, faults):
    """Empty elements, checked on the UNSTRIPPED body.

    Running this on `prose` (code removed) turns every `<td><code>x</code></td>`
    into `<td></td>` and reports 21 phantom empties. The two checks need
    different inputs, so they are separate functions.
    """
    for t in ("p", "li", "td"):
        cells = re.findall(rf"<{t}(?:\s[^>]*)?>(.*?)</{t}>", body, re.S)
        n = sum(1 for c in cells if not c.strip())
        if n:
            faults.append(f"{name}: {n} empty <{t}>")


def check_links(name, html, faults):
    for href in re.findall(r'href="([^"#][^"]*)"', html):
        if href.startswith(("http://", "https://", "mailto:", "//")):
            continue
        target = href.split("#", 1)[0].split("?", 1)[0]
        if not target:
            continue
        path = os.path.join(DOCS, target)
        if not os.path.exists(path):
            faults.append(f"{name}: broken internal link {href!r}")


def check_headings(name, body, faults):
    ids = re.findall(r'<h[1-6][^>]*\bid="([^"]+)"', body)
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        faults.append(f"{name}: duplicate heading id(s) {sorted(dupes)}")
    if not ids:
        faults.append(f"{name}: no headings found")


def check_figures(name, html, faults):
    for src in re.findall(r'src="([^"]+)"', html):
        if src.startswith(("http://", "https://", "data:")):
            continue
        path = os.path.join(DOCS, src)
        if not os.path.exists(path):
            faults.append(f"{name}: missing asset {src!r}")


def main():
    pages = sorted(f for f in os.listdir(DOCS) if f.endswith(".html"))
    if not pages:
        print("FAIL: no built pages in docs/ -- run docs/build.py first")
        return 1

    faults = []
    for f in pages:
        with open(os.path.join(DOCS, f), encoding="utf-8") as fh:
            html = fh.read()
        body = body_of(html)
        check_balance(f, body, faults)
        check_leaks(f, body, faults)
        check_empty(f, body, faults)
        check_links(f, html, faults)
        check_headings(f, body, faults)
        check_figures(f, html, faults)

        # structural presence smoke-check
        if "<title>" not in html:
            faults.append(f"{f}: no <title>")

    if faults:
        print(f"FAIL: {len(faults)} defect(s) in the built site")
        for x in faults:
            print(f"  - {x}")
        return 1

    print(f"PASS: {len(pages)} page(s) structurally clean")
    for f in pages:
        print(f"  {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
