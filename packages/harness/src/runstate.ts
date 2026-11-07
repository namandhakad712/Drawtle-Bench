/**
 * Run lifecycle: status, atomic writes, checkpointing, and log health — a
 * port of legacy/python-v2/drawtle/runstate.ts with the same on-disk
 * formats. v2 runs remain readable; v3 runs are readable by v2.
 *
 * Design rules carried over verbatim:
 *  - a run has a status at all times, written before any work happens
 *  - every write is atomic (temp + rename, retried on a transient lock)
 *  - a malformed JSONL line is reported, never skipped silently
 *  - status is advisory: missing degrades to `unknown`, never `success`
 */

import * as nodeFs from "node:fs";
import * as nodePath from "node:path";
import type { Json } from "@drawtle/core";

export const STATUS_STARTED = "started";
export const STATUS_SUCCESS = "success";
export const STATUS_ERROR = "error";
export const STATUS_INTERRUPTED = "interrupted";
export const STATUS_UNKNOWN = "unknown";

export const TERMINAL_OK: readonly string[] = [STATUS_SUCCESS];
export const TERMINAL_BAD: readonly string[] = [STATUS_ERROR, STATUS_INTERRUPTED];

export const MODE_LIVE = "live";
export const MODE_TEST = "test";
export const MODES: readonly string[] = [MODE_LIVE, MODE_TEST];

export const SAFE_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

/** The mock backend is a self-test by construction; anything else is live. */
export function inferMode(backendName?: string): "live" | "test" {
  return (backendName ?? "").trim().toLowerCase() === "mock" ? MODE_TEST : MODE_LIVE;
}

type Rec = Record<string, unknown>;

/** A run's mode, read from its own records. Returns [mode, source]. */
export function modeOf(
  status?: Rec | null,
  summary?: Rec | null,
): ["live" | "test", "recorded" | "inferred"] {
  for (const src of [status, summary]) {
    if (src && typeof src === "object" && MODES.includes(src.mode as string)) {
      return [src.mode as "live" | "test", "recorded"];
    }
  }
  let be: string | undefined;
  for (const src of [status, summary]) {
    if (src && typeof src === "object" && typeof src.backend === "string") {
      be = src.backend;
      break;
    }
  }
  return [inferMode(be), "inferred"];
}

export function slugify(name: unknown): string {
  const s = String(name ?? "")
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[-.]+|[-.]+$/g, "");
  return (s || "unknown").slice(0, 80);
}

export const IN_DIR: Record<string, string> = {
  jsonl: "run.jsonl",
  summary: "summary.json",
  status: "status.json",
  checkpoint: "done.json",
  report: "report.html",
};

export const FLAT_SUF: Record<string, string> = {
  jsonl: ".jsonl",
  summary: ".summary.json",
  status: ".status.json",
  checkpoint: ".done.json",
  report: ".html",
};

export function runDir(outDir: string, model: string, runId: string): string {
  return nodePath.join(outDir, slugify(model), runId);
}

export function dirPaths(d: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(IN_DIR)) out[k] = nodePath.join(d, v);
  return out;
}

export function flatPaths(outDir: string, runId: string): Record<string, string> {
  const base = nodePath.join(outDir, runId);
  const out: Record<string, string> = {};
  for (const [k, suf] of Object.entries(FLAT_SUF)) out[k] = base + suf;
  return out;
}

export function findRunDir(outDir: string, runId: string): string | null {
  if (!runId || !SAFE_RUN_ID.test(runId)) return null;
  let names: string[];
  try {
    names = nodeFs.readdirSync(outDir);
  } catch {
    return null;
  }
  for (const name of names) {
    const cand = nodePath.join(outDir, name, runId);
    try {
      if (nodeFs.statSync(cand).isDirectory()) return cand;
    } catch {
      /* keep looking */
    }
  }
  return null;
}

export function runPaths(outDir: string, runId: string, model?: string): Record<string, string> {
  if (model) return dirPaths(runDir(outDir, model, runId));
  const d = findRunDir(outDir, runId);
  if (d) return dirPaths(d);
  return flatPaths(outDir, runId);
}

export function layoutOf(outDir: string, runId: string): "dir" | "flat" | null {
  if (findRunDir(outDir, runId)) return "dir";
  for (const p of Object.values(flatPaths(outDir, runId))) {
    if (nodeFs.existsSync(p)) return "flat";
  }
  return null;
}

function runModel(outDir: string, runId: string): string | null {
  for (const key of ["summary", "status"]) {
    const p = flatPaths(outDir, runId)[key];
    if (!nodeFs.existsSync(p)) continue;
    try {
      const blob = JSON.parse(nodeFs.readFileSync(p, "utf8"));
      if (blob && typeof blob === "object" && typeof blob.model === "string") {
        return blob.model;
      }
    } catch {
      /* try next */
    }
  }
  return null;
}

export interface MigrationPlanItem {
  runId: string;
  from: string;
  to: string;
  model: string | null;
  files: string[];
}

export function planMigration(outDir: string): MigrationPlanItem[] {
  const plan: MigrationPlanItem[] = [];
  let names: string[];
  try {
    names = nodeFs.readdirSync(outDir);
  } catch {
    return plan;
  }
  for (const suf of [".summary.json", ".status.json"]) {
    for (const name of names) {
      if (!name.endsWith(suf)) continue;
      const runId = name.slice(0, -suf.length);
      if (!SAFE_RUN_ID.test(runId)) continue;
      if (findRunDir(outDir, runId)) continue; // already nested
      const model = runModel(outDir, runId);
      if (!model) continue; // cannot place it without a model
      const to = runDir(outDir, model, runId);
      plan.push({
        runId,
        from: outDir,
        to,
        model,
        files: Object.values(flatPaths(outDir, runId)),
      });
    }
  }
  return plan;
}

export function migrateFlatRuns(outDir: string, apply = false): MigrationPlanItem[] {
  const plan = planMigration(outDir);
  if (!apply) return plan;
  for (const item of plan) {
    nodeFs.mkdirSync(nodePath.dirname(item.to), { recursive: true });
    for (const f of item.files) {
      if (!nodeFs.existsSync(f)) continue;
      nodeFs.renameSync(f, nodePath.join(item.to, nodePath.basename(f)));
    }
  }
  return plan;
}

// ----------------------------------------------------------- atomic writes --

let tmpCounter = 0;

function tmpName(path: string): string {
  tmpCounter += 1;
  return `${path}.${process.pid}.${tmpCounter}.${Date.now()}.tmp`;
}

function replaceWithRetry(tmp: string, path: string): void {
  let last: unknown = null;
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      nodeFs.renameSync(tmp, path);
      return;
    } catch (e) {
      const code = (e as NodeJS.ErrnoException).code;
      if (code === "EPERM" || code === "EACCES" || code === "EBUSY") {
        last = e;
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50 * (attempt + 1));
        continue;
      }
      throw e;
    }
  }
  throw last;
}

/** Write JSON so a concurrent reader sees either the old file or the new. */
export function atomicWriteJson(path: string, obj: unknown): string {
  const parent = nodePath.dirname(nodePath.resolve(path));
  nodeFs.mkdirSync(parent, { recursive: true });
  const tmp = tmpName(path);
  try {
    const text = prettyJson(obj as Json);
    const fh = nodeFs.openSync(tmp, "w");
    try {
      nodeFs.writeSync(fh, text);
      nodeFs.fsyncSync(fh);
    } finally {
      nodeFs.closeSync(fh);
    }
    replaceWithRetry(tmp, path);
  } catch (e) {
    try {
      nodeFs.rmSync(tmp, { force: true });
    } catch {
      /* best effort */
    }
    throw e;
  }
  return path;
}

import { pyDumps } from "@drawtle/core";

/** Python json.dump(obj, indent=2) layout, plus a trailing newline like
 *  json.dump leaves none — v2 wrote no trailing newline either. */
function prettyJson(x: Json): string {
  return indent2(x, 0);
}

function indent2(x: Json, d: number): string {
  const pad = "  ".repeat(d);
  const padIn = "  ".repeat(d + 1);
  if (Array.isArray(x)) {
    if (x.length === 0) return "[]";
    return `[\n${x.map((v) => padIn + indent2(v, d + 1)).join(",\n")}\n${pad}]`;
  }
  if (x !== null && typeof x === "object") {
    const keys = Object.keys(x as Record<string, Json>);
    if (keys.length === 0) return "{}";
    return `{\n${keys
      .map((k) => `${padIn}${JSON.stringify(k)}: ${indent2((x as Record<string, Json>)[k], d + 1)}`)
      .join(",\n")}\n${pad}}`;
  }
  return pyDumps(x as never);
}

// ----------------------------------------------------------------- status --

export function readStatusFile(path: string, runId: string): Rec {
  if (!nodeFs.existsSync(path)) {
    return {
      run_id: runId,
      status: STATUS_UNKNOWN,
      note: "no status file (run predates status tracking, or was started outside this tool)",
    };
  }
  try {
    return JSON.parse(nodeFs.readFileSync(path, "utf8")) as Rec;
  } catch (exc) {
    return {
      run_id: runId,
      status: STATUS_UNKNOWN,
      note: `status file unreadable: ${String(exc)}`,
    };
  }
}

export function readStatus(outDir: string, runId: string): Rec {
  return readStatusFile(runPaths(outDir, runId).status, runId);
}

export function statusOf(outDir: string, runId: string): string {
  return String(readStatus(outDir, runId).status ?? STATUS_UNKNOWN);
}

function iso(tsSeconds: number): string {
  const d = new Date(tsSeconds * 1000);
  const p = (n: number, w = 2) => String(n).padStart(w, "0");
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    `T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  );
}

export function markStarted(outDir: string, runId: string, meta: Rec): string {
  const rec: Rec = { ...(meta ?? {}), run_id: runId, status: STATUS_STARTED };
  const now = Date.now() / 1000;
  rec.started_at = now;
  rec.started_iso = iso(now);
  return atomicWriteJson(runPaths(outDir, runId).status, rec);
}

export function markFinished(
  outDir: string,
  runId: string,
  status: string,
  extra?: Rec,
  startedAt?: number,
): string {
  let rec = readStatus(outDir, runId);
  if (rec.status === STATUS_UNKNOWN && rec.note) {
    rec = { run_id: runId };
  }
  const now = Date.now() / 1000;
  rec.status = status;
  rec.finished_at = now;
  rec.finished_iso = iso(now);
  if (typeof startedAt === "number") {
    rec.wallclock_s = Math.round((now - startedAt) * 10) / 10;
  }
  Object.assign(rec, extra ?? {});
  return atomicWriteJson(runPaths(outDir, runId).status, rec);
}

export function markInterrupted(outDir: string, runId: string, extra?: Rec): string {
  return markFinished(outDir, runId, STATUS_INTERRUPTED, extra);
}

// ---------------------------------------------------------- checkpointing --

export function loadDone(
  outDir: string,
  runId: string,
): { done: Record<number, Rec>; datasetHash: string | null } {
  const p = runPaths(outDir, runId).checkpoint;
  if (!nodeFs.existsSync(p)) return { done: {}, datasetHash: null };
  try {
    const blob = JSON.parse(nodeFs.readFileSync(p, "utf8"));
    const done: Record<number, Rec> = {};
    for (const [k, v] of Object.entries(blob.done ?? {})) {
      done[Number(k)] = v as Rec;
    }
    return { done, datasetHash: blob.dataset_hash ?? null };
  } catch {
    return { done: {}, datasetHash: null };
  }
}

export function saveDone(
  outDir: string,
  runId: string,
  done: Record<number, Rec>,
  datasetHash: string | null,
): string {
  const sorted = Object.keys(done)
    .map(Number)
    .sort((a, b) => a - b);
  const out: Record<string, Rec> = {};
  for (const k of sorted) out[String(k)] = done[k];
  return atomicWriteJson(runPaths(outDir, runId).checkpoint, {
    run_id: runId,
    dataset_hash: datasetHash,
    done: out,
  });
}

// ------------------------------------------------------------- log health --

export interface JsonlReport {
  nLines: number;
  nRecords: number;
  badLines: Array<[number, string]>;
  truncatedTail: boolean;
  bytes: number;
  exists: boolean;
}

export function scanJsonl(path: string): { records: Rec[]; report: JsonlReport } {
  const records: Rec[] = [];
  const bad: Array<[number, string]> = [];
  let nLines = 0;
  if (!nodeFs.existsSync(path)) {
    return {
      records,
      report: {
        nLines: 0,
        nRecords: 0,
        badLines: [],
        truncatedTail: false,
        bytes: 0,
        exists: false,
      },
    };
  }
  const stat = nodeFs.statSync(path);
  const text = nodeFs.readFileSync(path, "utf8");
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (i === lines.length - 1 && line === "") break; // trailing newline
    if (!line.trim()) continue;
    nLines += 1;
    try {
      records.push(JSON.parse(line));
    } catch (exc) {
      bad.push([i + 1, String(exc)]);
    }
  }
  return {
    records,
    report: {
      nLines,
      nRecords: records.length,
      badLines: bad,
      truncatedTail: bad.length > 0 && bad[bad.length - 1][0] === nLines,
      bytes: stat.size,
      exists: true,
    },
  };
}

export function readJsonl(path: string, strict = true): Rec[] {
  const { records, report } = scanJsonl(path);
  if (strict && report.badLines.length) {
    const first = report.badLines[0];
    const hint = report.truncatedTail
      ? " The last line is the broken one, which is what a process killed mid-write looks like -- re-run with --resume to continue from the checkpoint."
      : "";
    throw new Error(
      `${path}: ${report.badLines.length} malformed line(s), first at line ${first[0]} (${first[1]}).${hint}`,
    );
  }
  return records;
}

export function deleteRun(
  outDir: string,
  runId: string,
): {
  removed: string[];
  missing: string[];
} {
  if (!runId || !SAFE_RUN_ID.test(runId)) {
    throw new Error(`refusing to delete ${JSON.stringify(runId)}: not a safe id`);
  }
  const paths = runPaths(outDir, runId);
  const removed: string[] = [];
  const missing: string[] = [];
  for (const p of Object.values(paths)) {
    if (nodeFs.existsSync(p)) {
      nodeFs.rmSync(p);
      removed.push(p);
    } else {
      missing.push(p);
    }
  }
  const dir = nodePath.dirname(paths.jsonl);
  try {
    if (nodeFs.readdirSync(dir).length === 0) {
      nodeFs.rmdirSync(dir);
      removed.push(dir);
    }
  } catch {
    /* non-empty or missing: fine */
  }
  return { removed, missing };
}
