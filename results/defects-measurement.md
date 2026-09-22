# Measurement core — audit findings and fix status

Mission M1 (audit) → M3 (fix). The M1 survey inspected the measurement core
(`drawtle/maze.py`, `measures.py`, `dataset.py`, `stats.py`, `render.py`,
`frames.py`, and the `analysis/` verification scripts) and filed 8 findings:
**0 P0, 4 P1, 4 P2**. This report records the disposition of each: FIXED with the
regression test that covers it, or DEFERRED with the reason.

Headline numbers re-measured after the fixes (see "Re-measured claims" below):
equivariance **239/240 (99.6%)**, unchanged; the correct-action-differs rate the
README describes is **59.5% (550/924)**, corrected from the 61.1% that a
different comparison yields.

---

## P0 — measurement-invalidating

**None.** The audit confirmed the scoring arithmetic is sound and the bench does
separate optimal (1.000) from one-turn-stale (0.316) over 60 episodes. No fix
was needed or made in this category — a clean P0 audit is a result, not a gap.

---

## P1 — correctness

### D-3 · `stats.rescale_input` skips the legacy token split on mixed logs — FIXED

**Location:** `drawtle/stats.py:76-100` (`rescale_input`), `drawtle/stats.py:53-73` (`_token_split`)

The guard `if not pin or pout: return pin, pout` was aggregate over the whole
log. On a **mixed** log — some records modern with `completion_tokens > 0`, some
legacy — the one modern record's output satisfied `pout` and the function
returned early, so every legacy record's input total was never split. The legacy
output share stayed on the input side: **input overstated, output understated by
the same amount.** `token_source` was `"mixed"`, which labels the log
untrustworthy, but the split was still silently skipped.

**Fix:** the split is now **per legacy record**, not per log. `_legacy_records()`
selects the records without `total_tokens` (the legacy marker), the guard is
"no legacy records present" rather than "any output recorded", and the estimate
is clamped against **the legacy input total only** so a modern record's tokens
can never be moved by an estimate that is not theirs. `_token_split` already
attributes each legacy total wholly to input and relies on this function to
correct it — that is exactly the path the old guard skipped.

**Regression test:** `analysis/test_measure_core.py` §1 — 12 assertions over
mixed / legacy-only / modern-only / no-prose / clamp-reach cases. Verified to
FAIL on the pre-fix guard (3 assertions) and PASS after.

**Behaviour, before → after** (legacy 1000 with 400 chars of prose + modern
50/30):

| log | `_token_split` | `rescale_input` before | `rescale_input` after |
|---|---|---|---|
| mixed | (1050, 30, "mixed") | (1050, 30) — split skipped | (950, 130) |
| legacy-only | (1000, 0, "estimated") | (900, 100) | (900, 100) — unchanged |
| modern-only | (50, 30, "measured") | (50, 30) | (50, 30) — unchanged |

The exact total is preserved in every case; the estimate only apportions it.

### D-4 · README's "61.1% of turns" is the wrong comparison — FIXED

**Location:** `README.md:64`; the claim was repeated in `PAPER.md:144`, `PAPER.md:1062`,
`METHODOLOGY.md:73`, and the built `docs/*.html`.

The README said "the correct action differs from the un-rotated world on 61.1%
of turns" and cited `results/semantics_check.json`. That file's `0.6115` is
**relabelled-vs-rigid** — two different world models compared *with each other* —
not relabelled-vs-unrotated. The comparison the sentence actually describes
measures **59.5% (550/924)**.

**Fix (two parts):**

1. `analysis/semantics_check.py` now computes and emits the comparison the prose
   names. A new `unrotated_reference()` gives the correct action in the base
   world; `main()` pairs it with `relabelled_reference()` over the same 924
   walked relabelled turns and records it as `relabelled_vs_unrotated`. Both
   rates are now in `results/semantics_check.json`, labelled in the console
   output, and the old `relabelled_vs_rigid` output explicitly says it is *not*
   the "differs from the un-rotated world" rate.
2. `README.md:64` now quotes **59.5% (550/924)** and names the
   `relabelled_vs_unrotated` key, while noting that the wider 61.1% in the same
   file is a different comparison.

**Regression test:** `analysis/test_measure_core.py` §2 — asserts the new key is
recorded, that both comparisons rate the same turn population, that the two
rates are distinct (locking them equal would defeat the fix), and that the
unrotated rate is the measured 550/924. Verified to FAIL against the pre-fix
`semantics_check.json` (3 assertions) and PASS after.

**Cross-mission follow-up:** `PAPER.md` (§3.3 line 144, Appendix B line 1062) and
`METHODOLOGY.md:73` repeat the defective 61.1% claim, and the generated
`docs/index.html`, `docs/methodology.html`, `docs/overview.html` render it. Those
files are **outside M3's scope** and were reverted rather than committed. They
should be corrected by a mission that owns them — the fix is the same wording as
README.md:64, plus an Appendix B entry recording the correction. The source of
truth (`analysis/semantics_check.py` + `results/semantics_check.json`) now emits
both numbers, so a docs mission can cite them directly.

---

## P2 — quality

### D-8 · killtest SUMMARY prints 5.7x while its own TEST 5 table computes 7.3x — FIXED

**Location:** `analysis/killtest.py:380-391` (was `:204` summary vs `:162` table); output `results/killtest.txt`

The TEST 5 table computes a 90-degree rotation at **7.3x** the turtle's own move
(284.4px / 38.7px), but the SUMMARY printed **5.7x** from a per-maze value
returned by `test_masking` — the last maze's ratio, not the mean the table
averages. A reader quoting the summary quoted a number the same document
contradicted.

**Fix:** `test_masking` now computes the **mean** 90-degree displacement over the
same 12 mazes the table averages, so the summary line and the table interpolate
one computed value. `results/killtest.txt` regenerated; table and summary now
both read **7.3x**.

**Regression test:** `analysis/test_measure_core.py` §3 — parses the table's 90d
ratio and the SUMMARY's ratio out of `results/killtest.txt` and asserts they
agree (and equal the table's 7.3x). Verified to FAIL against the pre-fix
`results/killtest.txt` (1 assertion) and PASS after.

### D-5 · Only 4 distinct frames per probe episode (27.6% of turns repeat the previous frame) — DEFERRED

**Location:** `drawtle/frames.py` / probe episode construction; reported against `README.md:78-86`

Not a code defect. It is a **property of the bench**: with the rotation
vocabulary and the probe's stationary turtle, a typical episode reuses ~4
distinct frames and 27.6% of turns repeat the previous frame exactly, so a stale
frame is often still correct. Widening the rotation vocabulary would change what
the bench measures, which is a design decision for `PAPER.md` §3.3/§9, not a
bug to fix in a defect mission. The four-frame sequence in `README.md:78-86` is a
best case, not a typical one — a prose precision point, and out of M3's file
scope apart from README.md.

**Action for the tower:** route as a design/`PAPER.md` follow-up. If taken up,
report distinct frames per episode in the summary so the dilution is visible
rather than implicit.

### D-6 · MDI is a bench property presented beside model scores — DEFERRED

**Location:** `results/bench_properties.json`, dashboard/report layer

MDI (0.478, flat across lag 1–3) measures the instrument's discrimination against
a **synthetic** stale policy, not a property of any model. Presenting it beside
model scores invites the misreading that it is a per-model measurement. That is
a **presentation-layer** issue in the summary/dashboard fields, not a defect in
the measurement core's arithmetic.

**Action for the tower:** route to a mission owning the report layer — rename to
`bench_mdi` or carry `mdi_source`, and record which dataset and mode produced the
stale curve.

### D-7 · Committed mock runs record a `dataset_hash` no current manifest produces — DEFERRED

**Location:** `results/mock-opt.jsonl`, `results/mock-stale.jsonl` + their summaries;
hash `sha256:d8ceb927aa881366`

The committed mock-run fixtures record a dataset hash that no current manifest
generates, so `leaderboard(dataset=...)` cannot match them. The fixtures are
**gitignored legacy mock data**, not measurements of anything; rewriting their
hashes would be editing evidence, and regenerating them is a fixture-management
task rather than a measurement-core defect.

**Action for the tower:** regenerate or retire the legacy mock fixtures as a
fixtures task. The filter itself was audited and is correct (prefix match, and
a run with no recorded hash is excluded whenever a filter is set — unproven, not
zero).

---

## Routed to M4 (execution core, not fixed here)

The two most severe M1 findings live in `drawtle/runner.py`, which is M4's scope.
They are recorded here because they were filed by the measurement-core audit, but
**no change was made to `drawtle/runner.py` in this mission.**

- **D-1 · probe mode (`navigate=False`)** — the turtle never moves; progress
  measures heading-drift against a maze that re-forms around a stationary point;
  and in 58/200 episodes a rotated exit lands on the fixed entry cell, ending the
  episode early with `completion=True` and `efficiency=optimal_len/0` steps.
  `drawtle/runner.py:519-528, 599-601, 684`. **Routed to M4.**
- **D-2 · `Runner._SCHED` is a class attribute** — multiple Runners in one
  process silently share one rotation schedule, so `run_id` is ignored in-process
  and runs are not reproducible cross-process. `drawtle/runner.py:866-872`.
  **Routed to M4.**

---

## Re-measured claims

Every changed number was re-measured and the artifacts regenerated; nothing is
asserted that a script does not now print.

| Claim | Before | After fix | Source |
|---|---|---|---|
| Equivariance relation holds (rigid rotation) | 239/240 (99.6%) | **239/240 (99.6%)** — unchanged, 1 tie-break exception | `analysis/killtest.py` Test 1 |
| Correct action differs from the un-rotated world | "61.1%" (wrong comparison) | **59.5% (550/924)** | `analysis/semantics_check.py`, `relabelled_vs_unrotated` |
| Relabelled-vs-rigid disagreement | 61.15% (565/924) | **61.15% (565/924)** — unchanged, now correctly labelled | `relabelled_vs_rigid` |
| 90° rotation vs own move (masking) | table 7.3x / summary 5.7x | **7.3x in both** | `analysis/killtest.py` Test 5 + SUMMARY |
| Rigid invariance to deg | 945/960 (98.4%) | **945/960 (98.4%)** — unchanged | `rigid_invariance` |

The headline correction is honest and it moves **against** the README's old
claim: 59.5% < 61.1%. The bench's signal under Fix A is slightly weaker than the
prose stated, and the prose now quotes the comparison it names.

**Verified clean by the audit (no defect, no change):** heading is not drawn (the
disc SVG is byte-identical for heading 0 and 180); the dataset ladder is exact
prefixes of one seed (19/19 checks); the SVG→PNG cache is content-addressed;
unknown is never rendered as 0 in stats; bootstrap CI construction is correct.

---

## Verification

All scripts run from the repo root, standard library only, exit 0:

```
python analysis/test_measure_core.py     25 passed, 0 failed
python analysis/killtest.py              exit 0  (239/240, 7.3x)
python analysis/semantics_check.py       exit 0  (550/924, 565/924)
python analysis/test_datasets.py         19 passed, 0 failed
python analysis/make_figures.py          exit 0  (figures byte-identical to baseline)
python analysis/check_figures.py         4 figure(s) clean
python analysis/test_hosted_and_accounting.py  all accounting and registry tests passed
```

`make_figures.py` regenerates all four `figures/*.svg` **byte-identical** to the
pre-fix baseline (md5 unchanged), confirming the measurement-core fixes did not
perturb the render path. `test_hosted_and_accounting.py` — the pre-existing
coverage for `_token_split` / `rescale_input`, including the real legacy log
`results/mock-opt.jsonl` (total preserved at 55722, source "estimated") — still
passes, so the fix did not regress the legacy-only path.
