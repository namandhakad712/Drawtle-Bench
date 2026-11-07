/**
 * Run supervisor: launches `drawtle run` as a child process, streams its
 * JSONL over SSE, and stops it at the next episode boundary.
 *
 * The board never runs bench code in its own process: a run is a CLI child
 * (same lifecycle, same artifacts, same resume semantics), supervised only
 * through the filesystem it writes and SIGINT.
 */

import * as nodeFs from "node:fs";
import * as nodePath from "node:path";
import { STATUS_STARTED, readStatus, runPaths } from "@drawtle/harness";
import type { Subprocess } from "bun";

export interface Job {
  runId: string;
  proc: Subprocess;
  startedAt: number;
}

const jobs = new Map<string, Job>();

export function activeJobs(): Array<{ run_id: string; pid: number; started_at: number }> {
  return [...jobs.values()].map((j) => ({
    run_id: j.runId,
    pid: j.proc.pid,
    started_at: j.startedAt,
  }));
}

export interface LaunchSpec {
  backend: string;
  model: string;
  dataset: string;
  mode?: string;
  lag?: number;
  navigate?: boolean;
  limit?: number;
  runId?: string;
  runMode?: "live" | "test";
  effort?: string;
  maxTurns?: number;
  warmup?: boolean;
  framesDir?: string;
}

export function launchRun(
  cliPath: string,
  resultsDir: string,
  spec: LaunchSpec,
): { run_id: string; cmd: string[] } {
  if (jobs.size >= 4) {
    throw new Error("refusing to start more than 4 concurrent runs");
  }
  const runId =
    spec.runId && /^[\w.-]{1,64}$/.test(spec.runId)
      ? spec.runId
      : `run-${spec.model}-${Math.floor(Date.now() / 1000)}`;
  if (jobs.has(runId)) throw new Error(`run ${runId} is already active`);
  const args = [
    "run",
    "--backend",
    spec.backend,
    "--model",
    spec.model,
    "--dataset",
    spec.dataset,
    "--run-id",
    runId,
    "--out-dir",
    resultsDir,
  ];
  if (spec.mode) args.push("--mode", spec.mode);
  if (spec.lag) args.push("--lag", String(spec.lag));
  if (spec.navigate) args.push("--navigate");
  if (spec.limit && spec.limit > 0) args.push("--limit", String(spec.limit));
  if (spec.runMode) args.push("--run-mode", spec.runMode);
  if (spec.effort) args.push("--effort", spec.effort);
  if (spec.maxTurns) args.push("--max-turns", String(spec.maxTurns));
  if (spec.warmup === false) args.push("--no-warmup");
  if (spec.backend !== "mock") {
    args.push("--frames", spec.framesDir ?? nodePath.join(resultsDir, "frames"));
  }
  const cmd = ["bun", cliPath, ...args];
  const proc = Bun.spawn(cmd, {
    stdout: "pipe",
    stderr: "pipe",
    stdin: "ignore",
    env: { ...process.env, DRAWTLE_BOARD: "1" },
  });
  jobs.set(runId, { runId, proc, startedAt: Date.now() });
  // Reap when it exits on its own.
  void proc.exited.then((code) => {
    jobs.delete(runId);
    if (code !== 0 && code !== 130) {
      console.error(`[board] run ${runId} exited with code ${code}`);
    }
  });
  return { run_id: runId, cmd };
}

export function killRun(
  resultsDir: string,
  runId: string,
): {
  stopped: boolean;
  note: string;
} {
  const job = jobs.get(runId);
  if (job) {
    job.proc.kill("SIGINT");
    return {
      stopped: true,
      note: "SIGINT sent; the run stops at the next boundary and records status=interrupted",
    };
  }
  // Not one of ours (maybe started from a terminal). Ask the status file.
  const st = readStatus(resultsDir, runId);
  if (st.status === STATUS_STARTED) {
    return {
      stopped: false,
      note: "not started by this board; stop it in the terminal that owns it (Ctrl-C), or it will be marked interrupted on exit",
    };
  }
  return { stopped: false, note: `status is ${String(st.status)}; nothing to stop` };
}

/** SSE stream of a run's JSONL + status, ending on a terminal status. */
export function streamRun(
  resultsDir: string,
  runId: string,
  send: (event: string, data: unknown) => void,
): () => void {
  const paths = runPaths(resultsDir, runId);
  const jsonl = paths.jsonl;
  let offset = 0;
  let closed = false;
  const TERMINALS = new Set(["success", "error", "interrupted"]);

  const pump = (): void => {
    if (closed) return;
    try {
      if (!nodeFs.existsSync(jsonl)) return;
      const size = nodeFs.statSync(jsonl).size;
      if (size > offset) {
        const fh = nodeFs.openSync(jsonl, "r");
        const buf = Buffer.alloc(size - offset);
        nodeFs.readSync(fh, buf, 0, buf.length, offset);
        nodeFs.closeSync(fh);
        offset = size;
        for (const line of buf.toString("utf8").split("\n")) {
          if (!line.trim()) continue;
          try {
            send("turn", JSON.parse(line));
          } catch {
            /* partial tail line: wait for more */
          }
        }
      }
      const st = readStatus(resultsDir, runId);
      send("status", { status: st.status ?? "unknown", run_id: runId });
      if (TERMINALS.has(String(st.status))) {
        send("done", { status: st.status, run_id: runId });
        close();
      }
    } catch {
      /* transient */
    }
  };

  const watcher = nodeFs.watch(nodePath.dirname(jsonl), { persistent: false }, (_e, fname) => {
    if (fname === nodePath.basename(jsonl) || fname === nodePath.basename(paths.status)) {
      pump();
    }
  });

  const timer = setInterval(pump, 500);
  function close(): void {
    if (closed) return;
    closed = true;
    clearInterval(timer);
    try {
      watcher.close();
    } catch {
      /* already closed */
    }
  }
  pump();
  return close;
}
