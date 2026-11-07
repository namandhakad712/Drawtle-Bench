/**
 * Bootstrap confidence intervals — a byte-exact port of v2's
 * drawtle/stats.bootstrap_ci (the pure part).
 *
 * A benchmark score with no CI is not a result you can compare models on.
 * Resampling is seeded (PyRandom), so a re-score of the same log reproduces
 * the same interval to the last bit — the model is never re-called to
 * recompute a metric.
 */

import { PyRandom } from "./py";

function mean(xs: number[]): number | null {
  if (xs.length === 0) return null;
  let s = 0;
  for (const x of xs) s += x;
  return s / xs.length;
}

/**
 * 95% CI on the mean by resampling (percentile method). Values that are
 * null/undefined are dropped first, exactly as v2 dropped None.
 * Returns [lo, hi]; [null, null] when there is nothing to resample.
 */
export function bootstrapCi(
  values: Array<number | null | undefined>,
  n = 2000,
  seed: number | bigint = 0,
  alpha = 0.05,
): [number | null, number | null] {
  const vals = values.filter((v): v is number => v !== null && v !== undefined);
  if (vals.length < 2) {
    const m = mean(vals);
    return m === null ? [null, null] : [m, m];
  }
  const rng = new PyRandom(seed);
  const means: number[] = [];
  for (let i = 0; i < n; i++) {
    let s = 0;
    for (let j = 0; j < vals.length; j++) s += rng.choice(vals);
    means.push(s / vals.length);
  }
  means.sort((a, b) => a - b);
  const loPct = (alpha / 2) * 100;
  const hiPct = (1 - alpha / 2) * 100;
  const last = means.length - 1;
  return [
    means[Math.trunc((loPct / 100) * last)],
    means[Math.min(last, Math.trunc((hiPct / 100) * last))],
  ];
}
