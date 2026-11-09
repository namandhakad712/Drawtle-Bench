# Licensing

This repository ships two licenses, following the split the serious
benchmarks use (see `docs/BENCHMARK_LANDSCAPE.md` §Licenses for the survey):

| Scope | License | File |
|---|---|---|
| Code — `packages/`, `tools/`, `docker/`, workflows | **Apache-2.0** | [`LICENSE`](../LICENSE) |
| Generated artifacts — `results/dataset-*.json`, `results/levels.json`, `results/level_reference.json`, `results/bench_properties.json`, maze frames and figures | **CC BY 4.0** | https://creativecommons.org/licenses/by/4.0/ |
| Run artifacts — everything under `results/<model>/` | **CC BY 4.0** | https://creativecommons.org/licenses/by/4.0/ |
| `legacy/python-v2/` | unchanged from v2 history | see git history |

## Why this split

- **Apache-2.0 for code** rather than MIT: Apache-2.0 carries an explicit
  patent grant and a NOTICE mechanism. For an evaluation *platform* others
  will fork and extend, that is the more defensive default (Terminal-Bench,
  lm-evaluation-harness and MMMU chose likewise; MIT is the other common
  choice — SWE-bench, HLE, Inspect AI, MMLU-Pro).
- **CC BY 4.0 for datasets and run artifacts**: the mazes are *generated*
  from committed seeds — not scraped, not human-curated — so the most
  permissive attribution license keeps them maximally reusable and
  reproducible. The dataset hash makes attribution auditable: cite the hash.
- If you reuse the measurement, cite the repository and the specific
  dataset/levels hashes your numbers were produced against.

## Changing the license

Licensing is the repository owner's decision. To switch the code license to
MIT: replace `LICENSE` with the MIT text and update this file — nothing in
the code depends on license text. The two-license split itself is worth
keeping regardless of which code license you pick.
