# Migrating from v2 (Python) to v3 (Bun)

v3 is a port with proof, not a rewrite with opinions. The frozen v2 lives
in `legacy/python-v2/` and still runs from there. This page is what
changed, command by command.

## Commands

| v2 (`python bench.py …`) | v3 (`bun packages/cli/src/index.ts …` / `drawtle …`) |
| --- | --- |
| `datasets` | `datasets` (identical output, byte-for-byte) |
| `generate --count --sizes --seed --out` | same flags |
| `run --backend --model --dataset …` | same flags; `--frames` still required for vision, or `--text` for the blind text baseline (v3.2.1 — mutually exclusive with `--frames`) |
| `serve` | `serve` (or `bun packages/board/src/server.ts`) |
| `report --run --out` | same |
| `leaderboard` / `runs` / `status` / `cost` / `doctor` / `migrate` | same |
| — | `self-check` (5 gates), `compare --a --b`, `replay --run`, `key set/clear/list`, `models` |

Everything reads the same `results/` tree. Your v2 runs — including the
failed ones — keep working in every view; the parity test asserts the
enumeration is bit-identical to v2's.

## The one semantic change

**The rotation schedule.** v2 drew each turn's re-orientation from
`random.Random((hash(run_id) ^ (t * 2654435761)) & 0xffffffff)`. Python
salts `hash(str)` per process, so the same run id produced *different
schedules on every launch and every machine* — v2 runs were never
bit-reproducible even with the same seeds. v3 derives the per-turn seed
from sha256: identical distribution (15% silent turns, uniform
90/180/270), deterministic forever. Old runs are unchanged on disk; new
runs are reproducible. If you re-run a v2 run id under v3 you will get the
same *measurement semantics* with a stable schedule.

## The re-measured reference numbers

The audit on this branch found `results/bench_properties.json` was last
written by the initial v2 commit and never regenerated after commit
fcb71ff changed `progress_score` to score an absent action as False
instead of dropping the turn. Re-measured (frozen v2, same 40 seeds):

| quantity | published (stale) | re-measured |
| --- | --- | --- |
| optimal_progress | 1.000 | 0.926 |
| stale_by_lag 1/2/3 | 0.520 / 0.522 / 0.524 | 0.482 / 0.485 / 0.489 |
| MDI | 0.478 | **0.476** |

PAPER.md carries a dated corrigendum. The parity job in CI re-measures on
every change, so this class of staleness cannot recur. If you quoted MDI
0.478 anywhere, update to 0.476.

## Frames

v2 rasterised SVG→PNG with cairosvg (or a headless Chromium). v3 uses
resvg-js: a prebuilt Rust rasteriser — no system cairo, no browser, no
fonts (the frames are shapes only). Parity is proven at the SVG level
(what the frame hash in every turn record attests to); PNG bytes are
per-machine like before, and the cache is still keyed by the SVG hash, so
a cached frame is reused byte-for-byte. Vision runs made on one machine
keep their cached PNGs; run `drawtle doctor` to confirm rasterisation.

## Python

You do not need Python to run the bench. You need it only to regenerate
the parity fixtures (`python3 tools/parity/generate_golden.py`), which CI
does for you. The legacy tree stays runnable indefinitely for reference.

## Docker

Same compose file, same egress allow-list proxy; the bench image is now
`oven/bun:1.3.14-slim` with `bun install --frozen-lockfile` baked in. No
libcairo, no pip. `--build-arg UID=$(id -u)` as before. The mock smoke
command targets `results/dataset-20.json`.

## For developers

- `bun install` once; `bun test packages` (98 tests), `bunx tsc --noEmit`,
  `bunx biome check .`
- the parity fixtures are data: regenerate with
  `python3 tools/parity/generate_golden.py` and commit the diff *in the
  same change* that touches semantics — never "fix the fixtures later"
