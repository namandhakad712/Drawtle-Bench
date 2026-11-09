/**
 * Structured run event log — v3.1.
 *
 * Every run now keeps an append-only `events.jsonl` beside its run.jsonl:
 * one JSON object per lifecycle event (run_started, warmup, episode_start,
 * episode_done, token_cap, interrupted, error, run_finished). The run log
 * and the status sidecar stay the sources of truth for scoring — events are
 * diagnostics for humans and the board's Log panel, so writes are
 * best-effort and never fatal to a run.
 *
 * Format: {"ts": "2026-11-08T09:00:00", "ts_unix": 1791000000.0,
 *          "run_id": "...", "event": "episode_done", "severity": "info",
 *          ...event data}
 */

import * as nodeFs from "node:fs";
import * as nodePath from "node:path";
import { runPaths } from "./runstate";

export type EventSeverity = "info" | "warn" | "error";

export interface RunEvent {
  ts: string;
  ts_unix: number;
  run_id: string;
  event: string;
  severity: EventSeverity;
  [k: string]: unknown;
}

function iso(tsSeconds: number): string {
  const d = new Date(tsSeconds * 1000);
  const p = (n: number, w = 2) => String(n).padStart(w, "0");
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    `T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  );
}

export class EventLog {
  private fd: number | null = null;

  constructor(
    private path: string,
    private runId: string,
  ) {}

  log(event: string, data: Record<string, unknown> = {}, severity: EventSeverity = "info"): void {
    try {
      if (this.fd === null) {
        nodeFs.mkdirSync(nodePath.dirname(nodePath.resolve(this.path)), { recursive: true });
        this.fd = nodeFs.openSync(this.path, "a");
      }
      const now = Date.now() / 1000;
      const rec: RunEvent = {
        ts: iso(now),
        ts_unix: Math.round(now * 1000) / 1000,
        run_id: this.runId,
        event,
        severity,
        ...data,
      };
      nodeFs.writeSync(this.fd, `${JSON.stringify(rec)}\n`);
    } catch {
      /* diagnostics must never break a run */
    }
  }

  close(): void {
    if (this.fd !== null) {
      try {
        nodeFs.closeSync(this.fd);
      } catch {
        /* already closed */
      }
      this.fd = null;
    }
  }
}

export interface EventsReport {
  events: RunEvent[];
  path: string | null;
  exists: boolean;
  badLines: number;
}

/** Read a run's event log (best-effort parse; malformed lines are counted,
 *  never skipped silently). */
export function readEvents(outDir: string, runId: string): EventsReport {
  const path = runPaths(outDir, runId).events ?? null;
  if (!path || !nodeFs.existsSync(path)) {
    return { events: [], path, exists: false, badLines: 0 };
  }
  const events: RunEvent[] = [];
  let badLines = 0;
  for (const line of nodeFs.readFileSync(path, "utf8").split("\n")) {
    if (!line.trim()) continue;
    try {
      events.push(JSON.parse(line) as RunEvent);
    } catch {
      badLines += 1;
    }
  }
  return { events, path, exists: true, badLines };
}
