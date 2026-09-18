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
HERE = os.path.dirname(os.path.abspath(__file__))   # for resolving image paths

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

        # ---- figure: ![alt](src) with an optional caption ----
        # Deliberately narrow: an image must be alone on its line. Inline
        # images inside prose are not supported and are not used, and
        # guessing here would silently swallow surrounding text.
        mfig = re.match(r"^\s*!\[([^\]]*)\]\(([^)\s]+)\)\s*$", line)
        if mfig:
            alt, src = mfig.group(1), mfig.group(2)
            # The sources are repo-root relative ("docs/assets/img/x.png") so
            # that GitHub renders them from the repo root, but the built pages
            # live *inside* docs/. Strip the docs/ prefix or every image 404s
            # from the site while still working on github.com -- a split that
            # no amount of looking at either surface alone would reveal.
            web = src[5:] if src.startswith("docs/") else src
            body = [f'<img src="{html.escape(web)}" '
                    f'alt="{html.escape(alt, quote=True)}" loading="lazy">']

            # A caption is an explicitly italic-wrapped single line, either
            # `*text*` or `_text_`. It is NOT inferred from a following
            # paragraph: an earlier version guessed, and silently swallowed the
            # first line of the body text after every figure -- which cost the
            # README its opening sentence while still producing valid HTML.
            # Captions are marked, or there is no caption.
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n:
                e = re.match(r"^\s*[*_](?!\*)(.+?)(?<!\*)[*_]\s*$", lines[j])
                if e:
                    body.append(f"<figcaption>{inline(e.group(1))}</figcaption>")
                    i = j
            out.append("<figure>" + "".join(body) + "</figure>")
            i += 1
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
:root{
--bg:#fcfcfa;--bg2:#f6f6f2;--fg:#15171b;--mut:#5c6370;--faint:#8b919c;
--line:#e4e4dd;--line2:#d3d3ca;--pan:#ffffff;--code:#f5f5f0;
--acc:#8a4b1f;--acc2:#1c5c8a;--ok:#2c6e49;--warn:#96660a;--bad:#a32d2d;
--sh:0 1px 2px rgba(20,20,16,.04),0 4px 14px rgba(20,20,16,.045);
--r:10px;--r2:14px;
--mono:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace;
--sans:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
--serif:ui-serif,Georgia,'Times New Roman',serif}
@media (prefers-color-scheme:dark){:root{
--bg:#111310;--bg2:#171a15;--fg:#e9e7e0;--mut:#a0a6b0;--faint:#767c86;
--line:#282c24;--line2:#353a30;--pan:#171a15;--code:#1c2018;
--acc:#e0a878;--acc2:#84c8f0;--ok:#74d29a;--warn:#e3b467;--bad:#f08a8a;
--sh:0 1px 2px rgba(0,0,0,.3),0 4px 16px rgba(0,0,0,.25)}}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:16.5px/1.7 var(--serif);
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}

.masthead{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 88%,transparent);
backdrop-filter:saturate(180%) blur(12px);border-bottom:1px solid var(--line)}
.masthead .in{max-width:1340px;margin:0 auto;padding:11px 28px;display:flex;
align-items:center;gap:14px;font-family:var(--sans)}
.masthead .mk{font-weight:640;font-size:14.5px;letter-spacing:-.015em}
.masthead .mk a{color:var(--fg);text-decoration:none}
.masthead .tag{color:var(--faint);font-size:11.5px;padding-left:13px;
border-left:1px solid var(--line2)}
.masthead .sp{margin-left:auto}
.masthead .gh{color:var(--mut);text-decoration:none;font-size:12.5px;
padding:5px 11px;border:1px solid var(--line2);border-radius:7px}
.masthead .gh:hover{color:var(--fg);border-color:var(--faint)}

.wrap{display:grid;grid-template-columns:264px minmax(0,1fr);max-width:1340px;
margin:0 auto;align-items:start}
nav{position:sticky;top:52px;max-height:calc(100vh - 52px);overflow-y:auto;
padding:30px 18px 60px 28px;border-right:1px solid var(--line);
font-family:var(--sans)}
nav .grp{color:var(--faint);font-size:10px;text-transform:uppercase;
letter-spacing:.1em;margin:22px 0 8px;font-weight:640}
nav .grp:first-child{margin-top:0}
nav a{display:block;color:var(--mut);text-decoration:none;font-size:13.2px;
padding:5px 10px;border-radius:7px;margin-left:-10px;line-height:1.4}
nav a:hover{color:var(--fg);background:var(--bg2)}
nav a.on{color:var(--acc);font-weight:600;background:var(--bg2)}
nav .toc a{font-size:12.4px;padding:3.5px 10px;color:var(--mut)}
nav .toc a.sub{padding-left:22px;font-size:12px;color:var(--faint)}

main{padding:46px 56px 140px;min-width:0;max-width:820px}
.lede{font-size:1.06em;color:var(--mut);line-height:1.62;margin:0 0 1.9em}
h1,h2,h3,h4{font-family:var(--sans);line-height:1.24;letter-spacing:-.021em;
font-weight:640;scroll-margin-top:70px}
h1{font-size:2.1em;margin:0 0 .42em;letter-spacing:-.028em}
h2{font-size:1.44em;margin:2.3em 0 .6em;padding-bottom:.3em;border-bottom:1px solid var(--line)}
h3{font-size:1.14em;margin:1.85em 0 .48em}
h4{font-size:.98em;margin:1.4em 0 .38em;color:var(--mut);
text-transform:uppercase;letter-spacing:.05em;font-size:.82em}
.ah{opacity:0;margin-left:.42em;color:var(--faint);text-decoration:none;
font-size:.68em;font-weight:400;transition:opacity .12s}
h1:hover .ah,h2:hover .ah,h3:hover .ah,h4:hover .ah{opacity:.6}
a{color:var(--acc2);text-decoration-thickness:1px;text-underline-offset:2px}
p{margin:.8em 0}
strong{font-weight:640}
code{background:var(--code);padding:.14em .4em;border-radius:4px;
font:0.855em/1.45 var(--mono);border:1px solid var(--line)}
pre{background:var(--code);border:1px solid var(--line);border-radius:var(--r);
padding:16px 18px;overflow-x:auto;margin:1.2em 0;line-height:1.62}
pre code{background:none;padding:0;border:0;font-size:.845em}

.tw{overflow-x:auto;margin:1.3em 0;border:1px solid var(--line);
border-radius:var(--r);background:var(--pan)}
table{border-collapse:collapse;width:100%;font-family:var(--sans);font-size:13.3px}
th,td{padding:9px 13px;text-align:left;vertical-align:top;
border-bottom:1px solid var(--line)}
th{background:var(--bg2);font-weight:640;white-space:nowrap;font-size:12.4px;
letter-spacing:.01em;color:var(--fg)}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:color-mix(in srgb,var(--bg2) 55%,transparent)}
td code{font-size:12.4px}

figure{margin:1.9em 0}
/* Figures are 3:2-ish, generated at 1200px. Let them break out of the
   820px text measure and centre, capped by the viewport, so the maze stays
   legible. Text keeps its comfortable measure; the images get the room. */
figure img{width:100%;height:auto;display:block;border:1px solid var(--line);
border-radius:var(--r2);background:var(--pan)}
/* The figures are committed as opaque PNGs with a white ground, so in dark
   mode a full-brightness image glares out of the page. Dimming and slightly
   desaturating it is the standard treatment and keeps one set of assets for
   both themes -- the alternative is two rendered sets, which doubles the
   surface that can drift. */
@media (prefers-color-scheme:dark){
figure img{filter:brightness(.86) contrast(1.02) saturate(.92)}
}
@media(min-width:1180px){
figure{margin-left:-92px;margin-right:-92px}
figure figcaption{padding-left:94px;padding-right:94px}
}
figure figcaption{color:var(--mut);font-size:13px;font-family:var(--sans);
margin-top:.7em;line-height:1.55;padding-left:2px}

blockquote{margin:1.35em 0;padding:.85em 1.2em;border-left:3px solid var(--acc);
background:var(--bg2);border-radius:0 var(--r) var(--r) 0}
blockquote p{margin:.35em 0}
blockquote p:first-child{margin-top:0}
blockquote p:last-child{margin-bottom:0}
ul,ol{padding-left:1.55em;margin:.8em 0}
li{margin:.3em 0}
li>ul,li>ol{margin:.3em 0}
hr{border:0;border-top:1px solid var(--line);margin:2.8em 0}

.card{background:var(--pan);border:1px solid var(--line);border-radius:var(--r2);
padding:19px 22px;margin:1.5em 0;box-shadow:var(--sh)}
.card .k{font-family:var(--sans);font-size:11px;text-transform:uppercase;
letter-spacing:.09em;color:var(--faint);font-weight:640;margin-bottom:7px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:13px;
margin:1.7em 0}
.stat{background:var(--pan);border:1px solid var(--line);border-radius:var(--r);
padding:15px 17px}
.stat .v{font-family:var(--sans);font-size:1.62em;font-weight:640;
letter-spacing:-.024em;line-height:1.15}
.stat .l{font-family:var(--sans);font-size:11.5px;color:var(--mut);margin-top:4px;
line-height:1.42}
.stat.ok .v{color:var(--ok)}.stat.bad .v{color:var(--bad)}
.stat.acc .v{color:var(--acc)}

footer{color:var(--faint);font-size:12.6px;border-top:1px solid var(--line);
margin-top:5em;padding-top:1.5em;font-family:var(--sans);line-height:1.6}
footer a{color:var(--mut)}

@media(max-width:940px){.wrap{grid-template-columns:1fr}
nav{position:static;max-height:none;border-right:0;border-bottom:1px solid var(--line);
padding:18px 24px}nav .toc{display:none}main{padding:30px 22px 90px;max-width:none}
.masthead .tag{display:none}}
"""



def page(title, body, toc, pages, current, desc):
    nav = ['<nav>', '<div class="grp">Documentation</div>']
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
<header class="masthead"><div class="in">
<span class="mk"><a href="index.html">Drawtle Bench</a></span>
<span class="tag">Memory dominance in vision-language agents</span>
<span class="sp"></span>
<a class="gh" href="https://github.com/namandhakad712/Drawtle-Bench">GitHub</a>
</div></header>
<div class="wrap">
{chr(10).join(nav)}
<main>
{body}
<footer>
<p><strong>Drawtle Bench v2.0.0</strong> — the instrument is validated; the
measurement is not. No real model has been run yet. Every number in these docs
comes from reference policies, and <code>PAPER.md</code> §8.2 says so.</p>
<p><a href="https://github.com/namandhakad712/Drawtle-Bench">Source</a> ·
<a href="testing.html">Testing</a> ·
<a href="methodology.html">Methodology</a> ·
<a href="index.html">Paper</a></p>
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
