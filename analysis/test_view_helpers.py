"""Guard: the dashboard's number helpers must match their Python twins.

The control centre renders numbers in two places -- server-side for the run
report pages, and client-side for the single-page dashboard -- and the five
formatting helpers (`dash`, `num`, `pct`, `money`, `tag`) are written twice, once
in Python and once in the inline `<script>`.

That duplication is a correctness hazard, not just untidiness, because these
helpers are where this project's central rule lives:

    **Unknown is not zero.** A `null` renders as a dash with a tooltip. A real
    `0` renders as `0`.

If the two implementations drift, one half of the UI starts reporting an
unmeasured value as a zero -- a false claim about a model -- and nothing else in
the suite would notice, because both halves look internally consistent.

So this runs both implementations over the same inputs and compares the output
byte for byte. It skips (exit 0) when node is unavailable.

Run:  python analysis/test_view_helpers.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from web import views as V          # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"\n         {detail}" if not cond and detail else ""))


def _extract_js_helpers(page):
    """Pull the helper function sources out of the emitted page.

    Brace matching rather than a regex, because the bodies contain braces (in
    the `toLocaleString` options and the template strings) and a non-greedy
    regex stops in the wrong place. The page also has top-level DOM calls and a
    boot IIFE, which is why the whole script cannot simply be run under node.
    """
    names = ("esc", "group", "dash", "tag", "pct", "num", "money")
    out = {}
    for name in names:
        m = re.search(r"^function " + name + r"\(", page, re.M)
        if not m:
            raise SystemExit(f"helper {name!r} not found in the emitted page")
        i = page.index("{", m.end())
        depth = 0
        for j in range(i, len(page)):
            if page[j] == "{":
                depth += 1
            elif page[j] == "}":
                depth -= 1
                if depth == 0:
                    out[name] = page[m.start(): j + 1]
                    break
        else:
            raise SystemExit(f"could not find the end of helper {name!r}")
    return out


#: Inputs chosen to hit the invariant from both sides: an unknown must not read
#: as a zero, and a real zero must not read as an unknown.
CASES = [
    ("dash", "dash(null)"), ("dash", "dash(undefined)"),
    ("dash", "dash(0)"), ("dash", "dash(5)"), ("dash", "dash('')"),
    ("pct", "pct(null)"), ("pct", "pct(undefined)"),
    ("pct", "pct(0)"), ("pct", "pct(0.5)"), ("pct", "pct(1)"),
    ("num", "num(null)"), ("num", "num(undefined)"),
    ("num", "num(0)"), ("num", "num(1234)"), ("num", "num(12.5)"),
    ("num", "num(7, ' turns')"),
    ("money", "money(null, true)"), ("money", "money(undefined, true)"),
    ("money", "money(0, true)"), ("money", "money(1.2345, true)"),
    ("money", "money(5, false)"),
    ("tag", "tag('ok', 'ok')"), ("tag", "tag('x', 'err', 'why')"),
    ("tag", "tag('y')"),
]

PY_CALLS = {
    "dash": lambda e: V.dash(_arg(e)),
    "pct": lambda e: V.pct(_arg(e)),
    "num": lambda e: V.num(_arg(e), " turns") if "turns" in e else V.num(_arg(e)),
    "money": lambda e: V.money(_arg(e), "false" not in e),
    "tag": lambda e: (V.tag("x", "err", "why") if "err" in e
                      else V.tag("y") if "'y'" in e else V.tag("ok", "ok")),
}


def _arg(expr):
    """The literal argument inside a case expression, as a Python value."""
    inner = expr[expr.index("(") + 1: expr.rindex(")")]
    first = inner.split(",")[0].strip()
    if first == "null":
        return None
    if first == "undefined":
        return None
    if first.startswith("'"):
        return first.strip("'")
    return float(first) if "." in first else int(first)


def main():
    node = shutil.which("node")
    if not node:
        print("SKIP  view-helper parity: node not on PATH")
        return 0

    page = V.page("test", {})
    helpers = _extract_js_helpers(page)

    driver = "\n".join(helpers[n] for n in
                       ("esc", "group", "dash", "tag", "pct", "num", "money"))
    driver += "\nconst CASES = " + json.dumps([c[1] for c in CASES]) + ";\n"
    driver += ("console.log(JSON.stringify(CASES.map(e => "
               "String(eval(e)))));\n")

    fd, tmp = tempfile.mkstemp(prefix="view-helper-parity-", suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(driver)
        r = subprocess.run([node, tmp], capture_output=True, text=True)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if r.returncode != 0:
        check("the extracted helpers run under node", False, r.stderr[:400])
        return _report()
    js_out = json.loads(r.stdout)

    mismatches = []
    for (kind, expr), got in zip(CASES, js_out):
        want = PY_CALLS[kind](expr)
        if str(want) != str(got):
            mismatches.append(f"{expr}\n           python: {want!r}\n"
                              f"           js    : {got!r}")
    check(f"all {len(CASES)} helper outputs match between Python and JS",
          not mismatches, "\n         ".join(mismatches))

    # And the invariant itself, stated directly rather than only by agreement --
    # two implementations can agree on the wrong answer.
    unknown = ["dash(null)", "dash(undefined)", "pct(null)", "num(null)",
               "money(null, true)"]
    zero = ["dash(0)", "pct(0)", "num(0)", "money(0, true)"]
    idx = {expr: i for i, (_k, expr) in enumerate(CASES)}
    for expr in unknown:
        out = js_out[idx[expr]]
        check(f"{expr} is an unknown marker, not a zero",
              ("ndash" in out or "unknown" in out) and ">0<" not in out,
              repr(out))
    for expr in zero:
        out = js_out[idx[expr]]
        check(f"{expr} renders as a real zero, not a dash",
              "ndash" not in out and "unknown" not in out, repr(out))
    return _report()


def _report():
    print()
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"    FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
