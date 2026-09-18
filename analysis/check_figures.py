"""Lint the generated figures.

Visual inspection is not always available, and "I looked at it and it seemed
fine" is not evidence. This checks the failure modes that can be detected
mechanically:

  - the file is well-formed XML, with an svg root and a viewBox
  - every coordinate is finite
  - no text lands outside the canvas
  - no two text labels overlap

Text width is estimated as glyph count x font size x a per-family ratio. That
is an approximation; it is deliberately a little generous, so a collision it
reports is a collision.

    python analysis/check_figures.py          # lint, exit non-zero on fault
    python analysis/check_figures.py --selftest
"""
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIGS = os.path.join(ROOT, "figures")

WIDTH_RATIO = {"mono": 0.60, "serif": 0.50, "sans": 0.54}

NS = "{http://www.w3.org/2000/svg}"


def family_of(font_family):
    f = (font_family or "").lower()
    if "monospace" in f:
        return "mono"
    if "serif" in f:
        return "serif"
    return "sans"


def text_box(elem):
    """Approximate the bounding box of a <text> element, as (x0,y0,x1,y1)."""
    text = "".join(elem.itertext()) or ""
    if not text.strip():
        return None
    try:
        x = float(elem.get("x"))
        y = float(elem.get("y"))
    except (TypeError, ValueError):
        return None
    size = float(elem.get("font-size") or 13)
    fam = family_of(elem.get("font-family"))
    anchor = elem.get("text-anchor") or "start"
    # baseline is at y; the visual block sits roughly above it
    h = size * 1.16
    y0 = y - size * 0.92
    y1 = y + size * 0.24
    w = len(text) * size * WIDTH_RATIO[fam]
    if anchor == "middle":
        x0, x1 = x - w / 2.0, x + w / 2.0
    elif anchor == "end":
        x0, x1 = x - w, x
    else:
        x0, x1 = x, x + w
    return (x0, y0, x1, y1)


def overlaps(a, b, pad=1.0):
    return (a[0] < b[2] - pad and b[0] < a[2] - pad
            and a[1] < b[3] - pad and b[1] < a[3] - pad)


def numbers_ok(body):
    """Scan numeric attribute values for inf/nan. Float() accepts these, so a
    coordinate can silently become inf and render nowhere."""
    for m in re.finditer(r'"(-?\d*\.?\d+(?:[eE][-+]?\d+)?)"', body):
        try:
            v = float(m.group(1))
        except (ValueError, OverflowError):
            continue
        if math.isnan(v) or math.isinf(v):
            return f"non-finite value {m.group(1)!r}"
    return None


def lint(path):
    faults = []
    name = os.path.basename(path)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        body = raw.decode("utf-8")
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        faults.append(f"{name}: not well-formed XML ({exc})")
        return faults
    except OSError as exc:
        faults.append(f"{name}: unreadable ({exc})")
        return faults

    bad = numbers_ok(body)
    if bad:
        faults.append(f"{name}: {bad}")

    if not root.tag.startswith(NS):
        faults.append(f"{name}: root is not an svg element")
        return faults

    vb = root.get("viewBox")
    if not vb:
        faults.append(f"{name}: no viewBox")
        return faults
    try:
        parts = vb.split()
        vw, vh = float(parts[2]), float(parts[3])
    except (ValueError, IndexError):
        faults.append(f"{name}: bad viewBox {vb!r}")
        return faults

    boxes = []
    for elem in root.iter():
        tag = elem.tag.replace(NS, "")
        if tag != "text":
            continue
        bb = text_box(elem)
        if bb is None:
            continue
        if (bb[0] < -2 or bb[1] < -2 or bb[2] > vw + 2 or bb[3] > vh + 2):
            txt = "".join(elem.itertext())[:34]
            faults.append(f"{name}: text outside canvas at "
                          f"({bb[0]:.0f},{bb[1]:.0f})-({bb[2]:.0f},{bb[3]:.0f}) "
                          f"in a {vw:.0f}x{vh:.0f} viewBox: {txt!r}")
        boxes.append((bb, "".join(elem.itertext())))

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if overlaps(boxes[i][0], boxes[j][0]):
                faults.append(
                    f"{name}: labels collide: {boxes[i][1][:34]!r} "
                    f"vs {boxes[j][1][:34]!r}")
    return faults


def selftest():
    """Prove the linter can fail, on a copy with four injected faults."""
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="figselftest-")
    good = 0
    for f in sorted(os.listdir(FIGS)):
        if not f.endswith(".svg"):
            continue
        shutil.copy(os.path.join(FIGS, f), os.path.join(tmp, f))
        good += 1
    if good == 0:
        print("selftest: no figures to test")
        return 1

    target = os.path.join(tmp, sorted(os.listdir(tmp))[0])
    with open(target, "r", encoding="utf-8") as fh:
        t = fh.read()

    # 1. malformed XML
    broken = os.path.join(tmp, "broken.svg")
    with open(broken, "w", encoding="utf-8") as fh:
        fh.write(t + "<unclosed>")
    # 2. text outside the canvas
    off = t.replace("<text ", "<text ", 1)
    m = re.search(r'<text x="(-?[\d.]+)" y="(-?[\d.]+)"', off)
    if m:
        off = off.replace(m.group(0),
                          f'<text x="-400" y="-400"', 1)
    offname = os.path.join(tmp, "offcanvas.svg")
    with open(offname, "w", encoding="utf-8") as fh:
        fh.write(off)
    # 3. a deliberate collision: the same label duplicated, shifted by 2px
    collname = os.path.join(tmp, "collide.svg")
    first = re.search(r'<text[^>]*>[^<]+</text>', t)
    if first:
        shifted = re.sub(r'x="(-?[\d.]+)"',
                         lambda mm: f'x="{float(mm.group(1)) + 2:.1f}"',
                         first.group(0), count=1)
        with open(collname, "w", encoding="utf-8") as fh:
            fh.write(t.replace(first.group(0), first.group(0) + shifted))
    else:
        print("  selftest MISS  could not find a <text> to duplicate")
        ok = False
    # 4. a non-finite coordinate
    badnum = os.path.join(tmp, "nonfinite.svg")
    with open(badnum, "w", encoding="utf-8") as fh:
        fh.write(t.replace('x="30"', 'x="1e400"', 1))

    expected = {"broken.svg": "not well-formed",
                "offcanvas.svg": "outside canvas",
                "collide.svg": "collide",
                "nonfinite.svg": "non-finite"}
    ok = True
    for f, want in expected.items():
        got = lint(os.path.join(tmp, f))
        if not any(want in g for g in got):
            print(f"  selftest MISS  {f}: expected {want!r}, got {got}")
            ok = False
        else:
            print(f"  selftest ok    {f} -> {want}")
    shutil.rmtree(tmp, ignore_errors=True)
    if not ok:
        print("selftest FAILED: a fault class went undetected")
        return 1
    print("selftest ok      all four fault classes detected")
    return 0


def main(argv):
    if "--selftest" in argv:
        return selftest()

    if not os.path.isdir(FIGS):
        print(f"no figures directory at {FIGS}")
        return 1
    names = sorted(f for f in os.listdir(FIGS) if f.endswith(".svg"))
    if not names:
        print("no .svg files to lint")
        return 1

    faults = []
    for n in names:
        got = lint(os.path.join(FIGS, n))
        if got:
            faults.extend(got)
        else:
            print(f"  ok    {n}")

    print()
    if faults:
        print(f"{len(faults)} fault(s):")
        for f in faults:
            print("  " + f)
        return 1
    print(f"{len(names)} figure(s) clean: XML well-formed, coordinates finite, "
          f"no text outside the canvas, no label collisions")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
