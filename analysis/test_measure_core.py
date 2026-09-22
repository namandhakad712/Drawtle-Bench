"""Regression tests for the measurement core (maze / stats / semantics / killtest).

The three checks below exist because each guards a defect an audit found and a
fix corrected. They assert invariants, not individual numbers, and they are
assertable with plain:

    python analysis/test_measure_core.py

No pytest -- the repo runs PASS/FAIL checks the way `analysis/test_datasets.py`
does. Every failure prints the actual value alongside the expected one, because
a bare boolean tells the next reader nothing.
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from drawtle import stats as ST                    # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"  {mark}  {name}"
    if not cond and detail:
        line += f"\n         {detail}"
    print(line)


def _legacy(total, text=""):
    """A record from a pre-v2.5.0 log: prompt_tokens holds input+output."""
    return {"prompt_tokens": total, "completion_tokens": 0,
            "raw_model_text": text}


def _modern(pin, pout, text=""):
    """A record in the current schema: usage was measured by the provider."""
    return {"prompt_tokens": pin, "completion_tokens": pout,
            "total_tokens": pin + pout, "token_source": "measured",
            "raw_model_text": text}


# ------------------------------------------------- 1. rescale_input mixed log ---
#
# D-3 (P1): on a mixed log the guard `if not pin or pout` saw the one modern
# record's output, returned early, and left every legacy total attributed wholly
# to input. Input was overstated and output understated by the legacy output
# share. The split is now per legacy record.

def test_rescale_mixed_log():
    print()
    print("1. rescale_input splits the legacy records of a mixed log")
    # Legacy 1000 (input+output, 400 chars of prose) + modern (50 in, 30 out).
    # The modern record's 120 chars of prose must not be swept into the
    # estimate either: only legacy prose is.
    records = [_legacy(1000, "y" * 400), _modern(50, 30, "x" * 120)]

    pin, pout, src = ST._token_split(records)
    check("mixed log is labelled mixed", src == "mixed", f"src={src}")
    check("the modern record is counted on its own",
          (pin, pout) == (1050, 30), f"split=({pin},{pout})")

    pin2, pout2 = ST.rescale_input(pin, pout, records)
    # The legacy total is exact and must be preserved: nothing may be invented
    # or dropped by the estimate.
    check("mixed log preserves the exact total", pin2 + pout2 == pin + pout,
          f"{pin2}+{pout2} != {pin}+{pout}")
    # 400 chars / 4 chars-per-token = 100 tokens of estimated output, moved
    # from the input side to the output side.
    check("mixed log splits the legacy output share",
          (pin2, pout2) == (950, 130), f"rescaled=({pin2},{pout2})")
    check("mixed log actually moves tokens to output", pout2 > pout,
          f"{pout2} <= {pout}")
    check("mixed log reduces the input side", pin2 < pin, f"{pin2} >= {pin}")

    # The pure-legacy path is unchanged by the fix: same records, same result.
    legacy_only = [_legacy(1000, "y" * 400)]
    lp, lo, _ = ST._token_split(legacy_only)
    lp2, lo2 = ST.rescale_input(lp, lo, legacy_only)
    check("legacy-only log still splits the same way",
          (lp2, lo2) == (900, 100), f"legacy=({lp2},{lo2})")
    check("legacy-only log preserves the total", lp2 + lo2 == 1000,
          f"{lp2}+{lo2}")

    # The pure-modern path is unchanged: no legacy record, no estimate, no move.
    modern_only = [_modern(50, 30, "x" * 120)]
    mp, mo, msrc = ST._token_split(modern_only)
    mp2, mo2 = ST.rescale_input(mp, mo, modern_only)
    check("modern log is a no-op", (mp2, mo2) == (50, 30),
          f"modern=({mp2},{mo2})")
    check("modern log stays measured", msrc == "measured", f"src={msrc}")

    # No legacy prose to estimate from: nothing is invented, and the fallback
    # (cost._apportion) handles that case, not this function.
    bare = [_legacy(1000), _modern(50, 30)]
    bp, bo, _ = ST._token_split(bare)
    bp2, bo2 = ST.rescale_input(bp, bo, bare)
    check("legacy record with no prose estimates zero output",
          (bp2, bo2) == (1050, 30), f"bare=({bp2},{bo2})")

    # The clamp must see the legacy total only, never the modern records'
    # tokens: an estimate may not reach across records.
    clamped = [_legacy(100, "y" * 10000), _modern(1_000_000, 1_000_000)]
    cp, co, _ = ST._token_split(clamped)
    cp2, co2 = ST.rescale_input(cp, co, clamped)
    # est_out is clamped to legacy_in // 2 = 50, not to (pin // 2).
    check("the clamp uses the legacy total only",
          (cp2, co2) == (50 + 1_000_000, 50 + 1_000_000),
          f"clamped=({cp2},{co2})")


# --------------------------------------------- 2. the two rotation comparisons ---
#
# D-4 (P1): the README said "the correct action differs from the un-rotated
# world on 61.1% of turns" and cited semantics_check.json, whose 0.6115 is
# relabelled-vs-RIGID. The script now emits both comparisons, and the one the
# prose names is the one the prose quotes.

def test_semantics_two_comparisons():
    print()
    print("2. semantics_check emits the comparison the README names")
    p = os.path.join(ROOT, "results", "semantics_check.json")
    check("semantics_check.json exists", os.path.exists(p), p)
    if not os.path.exists(p):
        return
    with open(p, "r", encoding="utf-8") as fh:
        d = json.load(fh)

    check("relabelled_vs_unrotated is recorded",
          "relabelled_vs_unrotated" in d, str(sorted(d)))
    check("relabelled_vs_rigid is still recorded",
          "relabelled_vs_rigid" in d, str(sorted(d)))

    ru = d.get("relabelled_vs_unrotated") or {}
    rr = d.get("relabelled_vs_rigid") or {}
    # Both comparisons are over the same 924 walked relabelled turns, so their
    # denominators must agree -- the audit's 550/924 comes from that pairing.
    check("both comparisons rate the same turn population",
          ru.get("of") == rr.get("of"),
          f"unrotated.of={ru.get('of')} rigid.of={rr.get('of')}")

    # The two comparisons are two different questions, so two different rates.
    # Locking them equal would defeat the fix.
    check("the two rates are distinct numbers",
          ru.get("rate") != rr.get("rate"),
          f"unrotated={ru.get('rate')} rigid={rr.get('rate')}")
    check("the unrotated rate is the measured 550/924",
          (ru.get("n"), ru.get("of"), ru.get("rate")) == (550, 924, 0.5952),
          str(ru))

    # The headline figure the prose quotes must exist IN THE FILE the prose
    # cites. README.md names results/semantics_check.json for "differs from the
    # un-rotated world", so the fixture is regenerated from the script, not
    # hand-edited: the source of truth is analysis/semantics_check.py.
    src = os.path.join(HERE, "semantics_check.py")
    check("the script emits the unrotated key",
          "relabelled_vs_unrotated" in open(src, encoding="utf-8").read(), src)
    check("the unrotated reference function exists",
          "def unrotated_reference" in open(src, encoding="utf-8").read(), src)


# ---------------------------------------------------- 3. killtest self-agreement ---
#
# D-8 (P2): the SUMMARY printed a 90-degree masking ratio that disagreed with
# the TEST 5 table in the same output. The summary now interpolates the mean
# ratio over the same mazes the table averages, and the output is regenerated.

def test_killtest_summary_matches_table():
    print()
    print("3. killtest's SUMMARY agrees with its own TEST 5 table")
    p = os.path.join(ROOT, "results", "killtest.txt")
    check("killtest.txt exists", os.path.exists(p), p)
    if not os.path.exists(p):
        return
    with open(p, "r", encoding="utf-8") as fh:
        txt = fh.read()

    def table_ratio():
        """The '90d rotation' row's ratio, parsed out of the TEST 5 table."""
        for line in txt.splitlines():
            s = line.strip()
            if s.startswith("90d rotation") or "90d rotation" in s:
                parts = s.split()
                try:
                    return float(parts[-1].rstrip("x"))
                except ValueError:
                    continue
        return None

    def summary_ratio():
        """The ratio the SUMMARY line 5 prints."""
        lines = txt.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("5  A 90-degree rotation"):
                # The ratio is the token that ends in "x"; "90-degree" is a
                # number too and must not be mistaken for it.
                for w in " ".join(x.strip() for x in lines[i:i + 2]).split():
                    if w.endswith("x") and _is_num(w):
                        return float(w.rstrip("x"))
                return None
        return None

    tr, sr = table_ratio(), summary_ratio()
    check("the TEST 5 table reports a 90d ratio", tr is not None, txt[:200])
    check("the SUMMARY reports a 90d ratio", sr is not None, "no '5  A 90-degree' line")

    if tr is not None and sr is not None:
        check("the summary and the table agree",
              math.isclose(tr, sr, rel_tol=1e-6), f"table={tr} summary={sr}")
        check("the agreed ratio is the table's 7.3x",
              math.isclose(tr, 7.3, abs_tol=0.05), f"table={tr}")


def _is_num(w):
    s = w.rstrip("x")
    try:
        float(s)
        return True
    except ValueError:
        return False


def main():
    test_rescale_mixed_log()
    test_semantics_two_comparisons()
    test_killtest_summary_matches_table()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:")
        for n in FAIL:
            print(" -", n)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
