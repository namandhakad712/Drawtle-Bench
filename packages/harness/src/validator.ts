/**
 * Run-artifact validation — v3.2 "the trust release".
 *
 * A leaderboard is only as good as its weakest submitted artifact. This
 * module is the machine check behind the submission protocol
 * (SUBMISSION.md): given a run directory, recompute what the log alone
 * implies and compare it against what the summary claims, verify the status
 * sidecar against the event chain, and enforce the two structural guarantees
 * the leaderboard depends on — mocks never rank as live, and a `success`
 * status is only ever the explicit end of a completed lifecycle.
 *
 * Design rules, learned from the frontier (see docs/RESEARCH.md §2):
 *  - validation reads only committed artifacts — it never calls a model
 *  - every finding names its rule, so a failure is actionable, not mysterious
 *  - `error` fails the gate; `warn` is reported but does not. A run from
 *    before v3.1 has no event log — that is a warning, not a forgery.
 */

import * as nodeFs from "node:fs";
import * as nodePath from "node:path";
import { pyRound } from "@drawtle/core";
import { STATUS_SUCCESS, readJsonl, runPaths } from "./runstate";
import { enumerateRuns } from "./stats";

type Rec = Record<string, unknown>;

export type Severity = "error" | "warn";

export interface ValidationFinding {
  rule: string;
  severity: Severity;
  message: string;
}

export interface RunValidation {
  run_id: string;
  ok: boolean;
  errors: number;
  warnings: number;
  findings: ValidationFinding[];
}

function finding(rule: string, severity: Severity, message: string): ValidationFinding {
  return { rule, severity, message };
}

function readJsonSafe(path: string): Rec | null {
  try {
    return JSON.parse(nodeFs.readFileSync(path, "utf8")) as Rec;
  } catch {
    return null;
  }
}

/** Tolerant float compare: summaries round, logs do not. */
function close(a: number, b: number, tol = 5e-4): boolean {
  return Math.abs(a - b) <= tol;
}

export function validateRun(resultsDir: string, runId: string): RunValidation {
  const findings: ValidationFinding[] = [];
  const add = (rule: string, severity: Severity, message: string): void => {
    findings.push(finding(rule, severity, message));
  };

  const paths = runPaths(resultsDir, runId);

  // V1 — the log exists and every line carries the fields scoring needs.
  if (!nodeFs.existsSync(paths.jsonl)) {
    add(
      "V1-structure",
      "error",
      `${nodePath.basename(paths.jsonl)} does not exist — a run without its log cannot be validated`,
    );
    return finish(runId, findings);
  }
  let records: Rec[] = [];
  try {
    records = readJsonl(paths.jsonl, false);
  } catch (e) {
    add("V1-structure", "error", `run log is not parseable JSONL: ${String(e)}`);
    return finish(runId, findings);
  }
  if (records.length === 0) {
    add("V1-structure", "error", "run log is empty — nothing was measured");
    return finish(runId, findings);
  }
  let legacySchema = false;
  for (let i = 0; i < records.length; i++) {
    const r = records[i];
    if (!("progressed" in r) || !("episode" in r)) {
      add(
        "V1-structure",
        "error",
        `line ${i + 1} lacks the scoring fields (episode / progressed) — the log cannot be re-scored`,
      );
      break;
    }
    if (!("error_class" in r)) legacySchema = true;
  }
  if (legacySchema) {
    add(
      "V1-structure",
      "warn",
      "log carries no error_class — pre-v3 error taxonomy (legacy schema); re-scoring uses only the v2 fields",
    );
  }

  // What the log alone implies:
  const scored = records.filter((r) => r.progressed !== null && r.progressed !== undefined);
  const impliedRate =
    scored.length > 0
      ? scored.reduce((a, r) => a + (r.progressed ? 1 : 0), 0) / scored.length
      : null;
  const impliedEpisodes = new Set(records.map((r) => String(r.episode))).size;
  const impliedHit =
    records.length > 0 ? records.filter((r) => r.hit_wall === true).length / records.length : null;
  const impliedInvalid =
    records.length > 0 ? records.filter((r) => r.invalid === true).length / records.length : null;

  const summary = readJsonSafe(paths.summary);
  const status = readJsonSafe(paths.status);

  // V2 — the summary re-derives from the log (the anti-tamper core).
  if (summary === null) {
    add(
      "V2-summary",
      "warn",
      "no summary.json — the run cannot rank (the leaderboard already excludes it)",
    );
  } else {
    const pairs: Array<[string, number | null, number | null]> = [
      ["progress_rate", impliedRate, num(summary.progress_rate)],
      ["n_turns", records.length, num(summary.n_turns)],
      ["n_episodes", impliedEpisodes, num(summary.n_episodes)],
      ["hit_wall_rate", impliedHit, num(summary.hit_wall_rate)],
      ["invalid_rate", impliedInvalid, num(summary.invalid_rate)],
    ];
    for (const [name, implied, claimed] of pairs) {
      if (claimed === null && implied === null) continue;
      if (claimed === null || implied === null || !close(implied, claimed)) {
        add(
          "V2-summary",
          "error",
          `summary.${name} = ${claimed} but the log implies ${implied === null ? null : pyRound(implied, 4)} — the summary does not re-derive from the log`,
        );
      }
    }
  }

  // V3 — CI sanity: an interval must contain its own estimate.
  if (summary !== null) {
    const ci = summary.progress_ci95 as [number, number] | null | undefined;
    const rate = num(summary.progress_rate);
    if (Array.isArray(ci)) {
      const [lo, hi] = ci;
      if (
        typeof lo !== "number" ||
        typeof hi !== "number" ||
        lo > hi ||
        (rate !== null && (lo > rate + 5e-4 || hi < rate - 5e-4))
      ) {
        add(
          "V3-ci",
          "error",
          `progress_ci95 = [${lo}, ${hi}] is not a sane interval around ${rate}`,
        );
      }
    } else if (rate !== null) {
      add("V3-ci", "warn", "summary carries no progress_ci95");
    }
  }

  // V4 — status consistency. `success` must be the explicit end of a full
  // lifecycle, not a default that survived a crash.
  const statusField = status?.status ? String(status.status) : null;
  const summaryStatus = summary?.status ? String(summary.status) : null;
  if (statusField !== null && summaryStatus !== null && statusField !== summaryStatus) {
    add(
      "V4-status",
      "error",
      `status.json says '${statusField}' but summary says '${summaryStatus}' — the artifacts disagree about what happened`,
    );
  }
  if (statusField === STATUS_SUCCESS || summaryStatus === STATUS_SUCCESS) {
    const eventsOk = nodeFs.existsSync(paths.events);
    if (!eventsOk) {
      add(
        "V4-status",
        "warn",
        "status is success but there is no event log (pre-v3.1 run?) — the lifecycle cannot be re-verified",
      );
    } else {
      let started = false;
      let finished = false;
      try {
        for (const line of nodeFs.readFileSync(paths.events, "utf8").split("\n")) {
          if (!line.trim()) continue;
          const ev = JSON.parse(line) as Rec;
          if (String(ev.event) === "run_started") started = true;
          if (String(ev.event) === "run_finished") finished = true;
        }
      } catch {
        add("V4-status", "error", "event log is not parseable JSONL");
      }
      if (!started || !finished) {
        add(
          "V4-status",
          "error",
          `status is success but the event chain is incomplete (started=${started}, finished=${finished}) — 'success' is only ever explicit`,
        );
      }
    }
  }

  // V5 — mode stamping: a mock must never wear live colours.
  const model = String(records[0].model ?? status?.model ?? summary?.model ?? "");
  const backend = String(records[0].backend ?? status?.backend ?? summary?.backend ?? "");
  const isMock =
    model.toLowerCase() === "mock" ||
    backend.toLowerCase() === "mock" ||
    backend.toLowerCase() === "mocksdk";
  const mode =
    (status?.mode as string) ?? (summary?.mode as string) ?? (status?.run_mode as string) ?? null;
  if (isMock && mode !== null && mode !== "test") {
    add(
      "V5-mode",
      "error",
      `a mock run is stamped mode='${mode}' — mocks are structurally barred from ranking beside live runs`,
    );
  }
  if (!isMock && mode === null) {
    add("V5-mode", "warn", "run carries no mode stamp (pre-v2.9 run?)");
  }

  // V6 — cost honesty: an unknown price must not present as free.
  if (summary !== null) {
    const costKnown = summary.cost_known;
    const cost = num(summary.total_cost_usd);
    if (costKnown === false && cost !== null && cost > 0) {
      add(
        "V6-cost",
        "error",
        `cost_known is false but total_cost_usd = ${cost} — an unknown price must not present as a measured number`,
      );
    }
  }

  const errors = findings.filter((f) => f.severity === "error").length;
  const warnings = findings.length - errors;
  return {
    run_id: runId,
    ok: errors === 0,
    errors,
    warnings,
    findings,
  };
}

function num(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function finish(runId: string, findings: ValidationFinding[]): RunValidation {
  const errors = findings.filter((f) => f.severity === "error").length;
  return {
    run_id: runId,
    ok: errors === 0,
    errors,
    warnings: findings.length - errors,
    findings,
  };
}

/** Validate every run a leaderboard could see. Unknown-status runs are
 * included on purpose: a dead run that fails validation is a finding, not
 * something to hide. */
export function validateAll(resultsDir: string): RunValidation[] {
  const out: RunValidation[] = [];
  for (const runId of Object.keys(enumerateRuns(resultsDir)).sort()) {
    out.push(validateRun(resultsDir, runId));
  }
  return out;
}
