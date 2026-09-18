#!/usr/bin/env python3
"""Build the static documentation site into docs/ for GitHub Pages.

Zero dependencies -- no markdown library, no Node toolchain. GitHub Pages serves
docs/ straight from the branch, so the site must build with only a stock Python.
This is deliberate: a docs build that needs `pip install` is a docs build that
breaks in CI six months from now.

Supported (everything the repo's own docs use): ATX headings with anchors and a
generated table of contents, GFM tables with alignment, fenced code blocks,
blockquotes, ordered/unordered/nested lists, horizontal rules, inline
code/bold/italic/links, and an auto-linked issue/PR-style backtick reference pass.

Usage:  python docs/build.py
"""
import html
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")

# (source markdown, output html, nav label, short description)
PAGES = [
    ("PAPER.md", "index.html", "Paper", "The full technical treatment"),
    ("README.md", "overview.html", "Overview", "What this is and how to run it"),
    ("TESTING.md", "testing.html", "Testing", "How to run it, and what is verified"),
    ("METHODOLOGY.md", "methodology.html", "Methodology", "Metrics and measures"),
    ("DESIGN.md", "design.html", "Design", "Design rationale and kill test"),
    ("PLAN_v2.md", "plan.html", "Plan", "v2 delivery roadmap"),
    ("CHANGELOG.md", "changelog.html", "Changelog", "Version history"),
    ("CONTRIBUTING.md", "contributing.html", "Contributing", "How to contribute"),
]


# --------------------------------------------------------------- inline ----

def _slug(text):
    """Stable anchor id from heading text."""
    s = re.sub(r"<[^>]+>", "", text)
    s = s.lower()
    s = re.sub(r"[^\w\s.-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s or "section"


def inline(text):
    """Inline markdown -> html. Code spans are protected first so that
    ** and _ inside them survive untouched."""
    stash = []

    def keep(m):
        stash.append(m.group(1))
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"`([^`]+)`", keep, text)
    text = html.escape(text, quote=False)

    # links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                  r'<a href="\2">\1</a>', text)
    # bold then italic (order matters)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)

    # restore code spans
    def restore(m):
        return f"<code>{html.escape(stash[int(m.group(1))], quote=False)}</code>"

    return re.sub(r"\x00(\d+)\x00", restore, text)


# -------------------------------------------------------------- blocks ----

def render_table(rows):
    """rows: list of raw pipe-lines (the header, separator, and body)."""
    def cells(line):
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]

    header = cells(rows[0])
    sep = cells(rows[1]) if len(rows) > 1 else []
    aligns = []
    for c in sep:
        left, right = c.startswith(":"), c.endswith(":")
        aligns.append("center" if left and right else
                      "right" if right else
                      "left" if left else "")
    body = [cells(r) for r in rows[2:]]

    out = ['<div class="tw"><table>', "<thead><tr>"]
    for i, h in enumerate(header):
        a = f' style="text-align:{aligns[i]}"' if i < len(aligns) and aligns[i] else ""
        out.append(f"<th{a}>{inline(h)}</th>")
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>")
        for i in range(len(header)):
            c = row[i] if i < len(row) else ""
            a = f' style="text-align:{aligns[i]}"' if i < len(aligns) and aligns[i] else ""
            out.append(f"<td{a}>{inline(c)}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "\n".join(out)


def convert(md):
    """Markdown -> (html_body, toc_entries)."""
    lines = md.split("\n")
    out = []
    toc = []
    i = 0
    n = len(lines)
    # fenced code block accumulation
    while i < n:
        line = lines[i]

        # ---- fenced code ----
        if line.lstrip().startswith("```"):
            lang = line.lstrip()[3:].strip()
            buf = []
            i += 1
            while i < n and not lines[i].lstrip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = f' class="lang-{html.escape(lang)}"' if lang else ""
            code = html.escape("\n".join(buf), quote=False)
            out.append(f"<pre><code{cls}>{code}</code></pre>")
            continue

        # ---- table ----
        if line.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|?\s*$", lines[i + 1]):
            block = []
            while i < n and lines[i].startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(render_table(block))
            continue

        # ---- heading ----
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            # strip an explicit {#anchor} if present
            sid = None
            am = re.search(r"\{#([\w.-]+)\}\s*$", text)
            if am:
                sid = am.group(1)
                text = text[: am.start()].strip()
            sid = sid or _slug(text)
            out.append(f'<h{level} id="{sid}">{inline(text)}'
                       f'<a class="ah" href="#{sid}" aria-label="link">#</a></h{level}>')
            if level == 2:
                toc.append((sid, re.sub(r"<[^>]+>", "", text)))
            i += 1
            continue

        # ---- horizontal rule ----
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # ---- blockquote (consecutive > lines) ----
        if line.startswith(">"):
            buf = []
            while i < n and (lines[i].startswith(">") or
                             (lines[i].strip() == "" and i + 1 < n and lines[i + 1].startswith(">"))):
                buf.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            inner, _ = convert("\n".join(buf))
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        # ---- lists ----
        if re.match(r"^\s*([-*+]|\d+\.)\s+", line):
            block = []
            while i < n and (re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i]) or
                             (lines[i].strip() and lines[i].startswith(("  ", "\t")))):
                block.append(lines[i])
                i += 1
            out.append(render_list(block))
            continue

        # ---- blank ----
        if not line.strip():
            i += 1
            continue

        # ---- paragraph ----
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not re.match(
                r"^\s*([-*+]|\d+\.)\s+|^#|^>|^\||^\s*```", lines[i]):
            buf.append(lines[i])
            i += 1
        out.append(f"<p>{inline(' '.join(x.strip() for x in buf))}</p>")

    return "\n".join(out), toc


def render_list(block):
    """Nested-list renderer: 2-space or tab indent per level.

    Emits well-formed nesting. Two bugs were found by validating the output
    rather than eyeballing it:

    1. Sibling items emitted `</li></ul>` as one tag, double-counting `</li>`.
    2. A parent <li> containing a nested list was never closed, because the
       end-of-block close could only see the innermost open item. HTML permits
       omitting </li> before </ul>, so the page still rendered -- which is exactly
       why this has to be caught by counting tags, not by looking at the page.

    Both fixed by tracking the open-item stack explicitly and closing parents
    after their nested list closes.
    """
    out = []
    stack = []           # [(indent, ordered)] for OPEN lists
    items = []           # indent of each open <li>

    def close_item():
        if items:
            items.pop()
            out.append("</li>")

    def close_list():
        ordered = stack.pop()[1]
        out.append("</ol>" if ordered else "</ul>")

    def close_nested_to(depth):
        """Close nested lists (and their items) whose indent exceeds `depth`,
        then close the parent item that contained each one."""
        while stack and stack[-1][0] > depth:
            close_item()          # the last item of the nested list
            close_list()
            close_item()          # the parent <li> that held the nested list

    for raw in block:
        if not raw.strip():
            continue
        depth = len(raw) - len(raw.lstrip(" "))
        m = re.match(r"^\s*([-*+]|\d+\.)\s+(.*)$", raw)

        if not m:
            if items and out:
                out.append(" " + inline(raw.strip()))
            continue

        ordered = m.group(1)[0].isdigit()
        text = m.group(2).strip()

        if not stack:
            out.append("<ol>" if ordered else "<ul>")
            stack.append((depth, ordered))
        elif depth > stack[-1][0]:
            out.append("<ol>" if ordered else "<ul>")
            stack.append((depth, ordered))
        elif depth == stack[-1][0]:
            close_item()
        else:
            close_nested_to(depth)
            close_item()

        out.append(f"<li>{inline(text)}")
        items.append(depth)

    close_nested_to(-1)
    while stack:
        close_item()
        close_list()
    return "\n".join(out)


# ----------------------------------------------------------------- page ----

CSS = """
:root{--bg:#fbfbf9;--fg:#14161a;--mut:#5b6270;--line:#e2e2dc;--pan:#ffffff;
--acc:#8a4b1f;--acc2:#1f5f8a;--code:#f4f4f0;--warn:#a86a00;--ok:#2f7d4f}
@media (prefers-color-scheme:dark){:root{--bg:#12140f;--fg:#e8e6df;--mut:#9aa0ab;
--line:#2a2e26;--pan:#171a14;--acc:#e0a878;--acc2:#7fc4ef;--code:#1b1f18;
--warn:#e0b063;--ok:#6fce93}}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.65 ui-serif,Georgia,'Times New Roman',serif;-webkit-font-smoothing:antialiased}
.wrap{display:grid;grid-template-columns:250px minmax(0,1fr);gap:0;max-width:1280px;margin:0 auto}
nav{position:sticky;top:0;align-self:start;height:100vh;overflow-y:auto;
padding:26px 20px;border-right:1px solid var(--line);font-family:ui-sans-serif,system-ui,sans-serif}
nav .brand{font-weight:700;font-size:15px;letter-spacing:-.01em;margin-bottom:3px}
nav .brand a{color:var(--fg);text-decoration:none}
nav .sub{color:var(--mut);font-size:11.5px;margin-bottom:20px;line-height:1.45}
nav .grp{color:var(--mut);font-size:10px;text-transform:uppercase;letter-spacing:.09em;
margin:18px 0 7px;font-weight:600}
nav a{display:block;color:var(--mut);text-decoration:none;font-size:13px;
padding:4px 8px;border-radius:4px;margin-left:-8px}
nav a:hover{color:var(--fg);background:var(--pan)}
nav a.on{color:var(--acc);font-weight:600;background:var(--pan)}
nav .toc a{font-size:12.2px;padding:2.5px 8px;border-left:2px solid transparent;
border-radius:0 4px 4px 0}
nav .toc a:hover{border-left-color:var(--acc)}
main{padding:44px 48px 120px;min-width:0}
h1,h2,h3,h4{font-family:ui-sans-serif,system-ui,sans-serif;line-height:1.25;
letter-spacing:-.018em;font-weight:680}
h1{font-size:2.05em;margin:0 0 .5em}
h2{font-size:1.42em;margin:2.1em 0 .55em;padding-bottom:.28em;border-bottom:1px solid var(--line)}
h3{font-size:1.13em;margin:1.7em 0 .45em;color:var(--acc2)}
h4{font-size:1em;margin:1.3em 0 .35em;color:var(--mut)}
.ah{opacity:0;margin-left:.4em;color:var(--mut);text-decoration:none;font-size:.72em;font-weight:400}
h1:hover .ah,h2:hover .ah,h3:hover .ah,h4:hover .ah{opacity:.55}
a{color:var(--acc2)}
p{margin:.72em 0}
code{background:var(--code);padding:.13em .38em;border-radius:3px;
font:0.86em/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre{background:var(--code);border:1px solid var(--line);border-radius:7px;
padding:15px 17px;overflow-x:auto;margin:1.05em 0}
pre code{background:none;padding:0;font-size:.845em;line-height:1.6}
.tw{overflow-x:auto;margin:1.15em 0}
table{border-collapse:collapse;width:100%;font-family:ui-sans-serif,system-ui,sans-serif;
font-size:13.4px}
th,td{border:1px solid var(--line);padding:7px 11px;text-align:left;vertical-align:top}
th{background:var(--code);font-weight:640;white-space:nowrap}
tbody tr:nth-child(even){background:color-mix(in srgb,var(--code) 45%,transparent)}
blockquote{margin:1.15em 0;padding:.7em 1.1em;border-left:3px solid var(--acc);
background:var(--pan);border-radius:0 6px 6px 0}
blockquote p{margin:.3em 0}
ul,ol{padding-left:1.5em;margin:.7em 0}
li{margin:.24em 0}
hr{border:0;border-top:1px solid var(--line);margin:2.4em 0}
footer{color:var(--mut);font-size:12.5px;border-top:1px solid var(--line);
margin-top:4em;padding-top:1.3em;font-family:ui-sans-serif,system-ui,sans-serif}
@media(max-width:860px){.wrap{grid-template-columns:1fr}
nav{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}
nav .toc{display:none}main{padding:28px 20px 80px}}
"""


def page(title, body, toc, pages, current, desc):
    nav = ['<nav>',
           '<div class="brand"><a href="index.html">Drawtle Bench</a></div>',
           '<div class="sub">Memory dominance in<br>vision-language agents</div>',
           '<div class="grp">Documentation</div>']
    for _src, out, label, _d in pages:
        cls = ' class="on"' if out == current else ""
        nav.append(f'<a href="{out}"{cls}>{label}</a>')
    if toc:
        nav.append('<div class="grp">On this page</div><div class="toc">')
        for sid, text in toc:
            nav.append(f'<a href="#{sid}">{html.escape(text)}</a>')
        nav.append("</div>")
    nav.append("</nav>")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — Drawtle Bench</title>
<meta name="description" content="{html.escape(desc)}">
<meta property="og:title" content="{html.escape(title)} — Drawtle Bench">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:type" content="article">
<link rel="stylesheet" href="assets/site.css">
</head>
<body>
<div class="wrap">
{chr(10).join(nav)}
<main>
{body}
<footer>
Drawtle Bench v2.0.0 — instrument validated, measurement pending.
<a href="https://github.com/namandhakad712/Drawtle-Bench">Source on GitHub</a>.
</footer>
</main>
</div>
</body>
</html>
"""


INDEX_EXTRA = """
"""


def main():
    os.makedirs(os.path.join(DOCS, "assets"), exist_ok=True)
    with open(os.path.join(DOCS, "assets", "site.css"), "w", encoding="utf-8") as fh:
        fh.write(CSS)

    built = []
    for src, out, label, desc in PAGES:
        path = os.path.join(ROOT, src)
        if not os.path.exists(path):
            print(f"  skip  {src} (missing)")
            continue
        with open(path, "r", encoding="utf-8") as fh:
            md = fh.read()

        # strip a leading H1 -- the page template and nav carry the title context
        body, toc = convert(md)

        # page title from the source's own H1 if present, else the nav label.
        # The H1s in this repo are inconsistent ("Drawtle Bench — Methodology",
        # "DESIGN.md", "Contributing to Drawtle Bench"), so normalise to a bare
        # page name. The "{title} — Drawtle Bench" suffix is added once by the
        # page template, so it must NOT be added here.
        m = re.search(r"^#\s+(.*)$", md, re.M)
        title = re.sub(r"[*`]", "", m.group(1)).strip() if m else label
        title = re.sub(r"^Drawtle Bench\s*[—–-]\s*", "", title).strip()
        title = re.sub(r"\s*[—–-]\s*Drawtle Bench\s*$", "", title).strip()
        # "Contributing to Drawtle Bench" -> "Contributing": the name is mid-string
        # and the template appends it, so drop it wherever it sits.
        title = re.sub(r"\s+to Drawtle Bench\s*$", "", title).strip()
        if not title or title.lower() == "drawtle bench":
            title = label

        html_out = page(title, body, toc, PAGES, out, desc)
        with open(os.path.join(DOCS, out), "w", encoding="utf-8") as fh:
            fh.write(html_out)
        built.append((out, len(toc)))

    # .nojekyll keeps GitHub Pages from running Jekyll over the output
    with open(os.path.join(DOCS, ".nojekyll"), "w") as fh:
        fh.write("")

    print(f"built {len(built)} page(s) into docs/")
    for out, n in built:
        print(f"  {out:20s} {n} section(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
