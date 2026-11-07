/**
 * The v3 control centre API (versioned under /api/v3/). JSON everywhere;
 * every handler is in one table so the surface is auditable at a glance.
 *
 * Security posture (the v2 BETTERMENT audit M8/M10/M12, fixed by design):
 *  - binds 127.0.0.1 by default; an optional token gate for anything beyond
 *  - no docker control endpoint (report only); no shell-out from handlers
 *  - everything the client renders goes through Preact's escaping — no
 *    innerHTML, no string-built HTML on the client
 */

import * as nodeFs from "node:fs";
import * as nodePath from "node:path";
import {
  aggregateFull as aggregate,
  clearKey,
  compare as compareRuns,
  describeSettings,
  enumerateRuns,
  excludedRuns,
  leaderboard,
  loadBenchProperties,
  mask,
  mergedModels,
  modelInfo,
  providers,
  rasteriserStatus,
  readJsonl,
  resolveKey,
  responseCacheStats,
  runPaths,
  describe as sandboxDescribe,
  storeKey,
  toggleFavorite,
  writeSettings,
} from "@drawtle/harness";
import { activeJobs, killRun, streamRun } from "./supervisor";

type Rec = Record<string, unknown>;

export interface BoardCtx {
  resultsDir: string;
  rootDir: string;
  cliPath: string;
  launch: (spec: Rec) => { run_id: string; cmd: string[] };
}

type Handler = (
  req: Request,
  url: URL,
  ctx: BoardCtx,
  params: Record<string, string>,
) => Promise<Response> | Response;

function json(data: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(data), {
    ...init,
    headers: { "Content-Type": "application/json; charset=utf-8", ...(init?.headers ?? {}) },
  });
}

const notFound = (what: string) => json({ error: `${what} not found` }, { status: 404 });

function readJsonBody(req: Request): Promise<Rec> {
  return req
    .json()
    .catch(() => ({}))
    .then((b) => (typeof b === "object" && b !== null ? (b as Rec) : {}));
}

// ---------------------------------------------------------------- routes --

export const routes: Array<{ method: string; pattern: RegExp; keys: string[]; handler: Handler }> =
  [];

function route(method: string, path: string, handler: Handler): void {
  const keys: string[] = [];
  const rx = new RegExp(
    `^${path
      .split("/")
      .map((seg) => {
        if (seg.startsWith(":")) {
          keys.push(seg.slice(1));
          return "([^/]+)";
        }
        return seg.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      })
      .join("/")}$`,
  );
  routes.push({ method, pattern: rx, keys, handler });
}

route("GET", "/api/v3/system", (_req, _url, ctx) => {
  const sb = sandboxDescribe();
  return json({
    version: "3.0.0",
    runtime: `bun ${Bun.version}`,
    results_dir: ctx.resultsDir,
    sandbox: sb,
    settings: describeSettings(),
    active_runs: activeJobs(),
    response_cache: responseCacheStats(ctx.resultsDir),
    time: new Date().toISOString(),
  });
});

route("GET", "/api/v3/runs", (_req, url, ctx) => {
  const mode = url.searchParams.get("mode");
  const runs = enumerateRuns(ctx.resultsDir);
  const rows = Object.entries(runs).map(([rid, r]) => ({
    run_id: rid,
    status: r.status,
    status_source: r.statusSource,
    note: r.note,
    layout: r.layout,
    summary: r.summary
      ? {
          model: r.summary.model,
          backend: r.summary.backend,
          mode: r.summary.mode,
          progress_rate: r.summary.progress_rate,
          progress_ci95: r.summary.progress_ci95,
          progress_ci95_episode_cluster: r.summary.progress_ci95_episode_cluster,
          hit_wall_rate: r.summary.hit_wall_rate,
          invalid_rate: r.summary.invalid_rate,
          stale_rate: r.summary.stale_rate,
          n_turns: r.summary.n_turns,
          n_episodes: r.summary.n_episodes,
          total_cost_usd: r.summary.total_cost_usd,
          cost_known: r.summary.cost_known,
          total_tokens: r.summary.total_tokens,
          input_tokens: r.summary.input_tokens,
          output_tokens: r.summary.output_tokens,
          dataset_hash: r.summary.dataset_hash,
          mdi: r.summary.mdi,
          completion_rate: r.summary.completion_rate,
          mean_efficiency: r.summary.mean_efficiency,
          wallclock_s: r.summary.wallclock_s,
          transcript: r.summary.transcript,
        }
      : null,
    has_summary: r.summary !== null,
  }));
  const filtered = mode ? rows.filter((r) => r.summary?.mode === mode) : rows;
  return json({ runs: filtered, total: rows.length, shown: filtered.length });
});

route("GET", "/api/v3/run/:id", (_req, _url, ctx, p) => {
  const runs = enumerateRuns(ctx.resultsDir);
  const r = runs[p.id];
  if (!r) return notFound(`run ${p.id}`);
  const summary =
    r.summary ??
    aggregate(runPaths(ctx.resultsDir, p.id).jsonl, { run_id: p.id }, loadBenchPropertiesSafe(ctx));
  return json({
    run_id: p.id,
    status: r.status,
    status_source: r.statusSource,
    note: r.note,
    paths: relativize(r.paths, ctx),
    summary,
  });
});

route("GET", "/api/v3/run/:id/turns", (_req, url, ctx, p) => {
  const paths = runPaths(ctx.resultsDir, p.id);
  if (!nodeFs.existsSync(paths.jsonl)) return notFound(`log for ${p.id}`);
  const episode = url.searchParams.get("episode");
  const cap = Math.min(Number(url.searchParams.get("cap") ?? 4000), 20000);
  let records: Rec[] = [];
  try {
    records = readJsonl(paths.jsonl, false) as Rec[];
  } catch {
    return json({ error: "log unreadable" }, { status: 500 });
  }
  const filtered =
    episode !== null ? records.filter((r) => String(r.episode) === episode) : records;
  return json({
    run_id: p.id,
    total: records.length,
    shown: filtered.length,
    turns: filtered.slice(0, cap),
    truncated: filtered.length > cap,
  });
});

route("GET", "/api/v3/leaderboard", (_req, url, ctx) => {
  const mode = url.searchParams.get("mode");
  const dataset = url.searchParams.get("dataset");
  return json({
    rows: leaderboard(ctx.resultsDir, {
      onlyClean: true,
      mode: mode ?? null,
      dataset: dataset ?? null,
    }),
    excluded: excludedRuns(ctx.resultsDir),
  });
});

route("GET", "/api/v3/analytics", (_req, _url, ctx) => {
  const bp = loadBenchPropertiesSafe(ctx);
  const benchDemo = (() => {
    try {
      return JSON.parse(
        nodeFs.readFileSync(nodePath.join(ctx.resultsDir, "bench_demo.json"), "utf8"),
      );
    } catch {
      return null;
    }
  })();
  return json({
    bench_properties: bp,
    reference_rows: benchDemo,
    semantics: (() => {
      try {
        return JSON.parse(
          nodeFs.readFileSync(nodePath.join(ctx.resultsDir, "semantics_check.json"), "utf8"),
        );
      } catch {
        return null;
      }
    })(),
  });
});

route("GET", "/api/v3/complexity", (_req, _url, ctx) => {
  // size (and pair) vs progress across every clean run
  const rows = leaderboard(ctx.resultsDir, { onlyClean: true });
  const points: Array<{ model: string; size: number | string; progress_rate: number | null }> = [];
  for (const r of rows) {
    const runs = enumerateRuns(ctx.resultsDir);
    const entry = runs[r.run_id];
    const bySize = (entry?.summary?.by_size ?? []) as Array<Rec>;
    for (const b of bySize) {
      points.push({
        model: r.model ?? "?",
        size: (b.size as number | string) ?? "?",
        progress_rate: (b.progress_rate as number) ?? null,
      });
    }
  }
  return json({ points });
});

route("GET", "/api/v3/compare", (_req, url, ctx) => {
  const a = url.searchParams.get("a") ?? "";
  const b = url.searchParams.get("b") ?? "";
  if (!a || !b) return json({ error: "a and b are required" }, { status: 400 });
  const read = (id: string): Rec[] => {
    try {
      return readJsonl(runPaths(ctx.resultsDir, id).jsonl, false) as Rec[];
    } catch {
      return [];
    }
  };
  return json(compareRuns({ runId: a, records: read(a) }, { runId: b, records: read(b) }));
});

route("GET", "/api/v3/registry", (_req, url, _ctx) => {
  const q = (url.searchParams.get("q") ?? "").toLowerCase();
  const models = mergedModels();
  const out: Rec = {};
  for (const [id, m] of Object.entries(models)) {
    if (q && !id.toLowerCase().includes(q)) continue;
    out[id] = {
      provider: m.provider,
      context_window: m.context_window,
      max_output: m.max_output,
      price_in: m.price_in,
      price_out: m.price_out,
      price_known: m.price_known ?? false,
      capabilities: m.capabilities ?? m.modalities ?? [],
      matched: modelInfo(id).matched,
    };
  }
  return json({
    models: out,
    providers: Object.fromEntries(
      Object.entries(providers()).map(([name, spec]) => [
        name,
        {
          protocol: spec.protocol,
          auth: spec.auth,
          self_hosted: spec.self_hosted ?? false,
          rate_limit: spec.rate_limit,
          context_note: spec.context_note,
          usage_fields: spec.usage_fields,
          openai_compat: spec.openai_compat,
        },
      ]),
    ),
  });
});

route("GET", "/api/v3/keys", (_req, _url, _ctx) => {
  const rows = Object.keys(providers()).map((name) => {
    const { key, source } = resolveKey(name);
    return { backend: name, masked: key ? mask(key) : null, source };
  });
  return json({ keys: rows });
});

route("POST", "/api/v3/keys", async (req, _url, _ctx) => {
  const body = await readJsonBody(req);
  const backend = String(body.backend ?? "");
  const key = String(body.key ?? "");
  const action = String(body.action ?? "set");
  if (!backend) return json({ error: "backend required" }, { status: 400 });
  if (action === "clear") {
    clearKey(backend);
    return json({ ok: true, backend, action });
  }
  if (!key) return json({ error: "key required" }, { status: 400 });
  storeKey(backend, key);
  return json({ ok: true, backend, action: "set", masked: mask(key) });
});

route("GET", "/api/v3/settings", (_req, _url, _ctx) => json(describeSettings()));

route("POST", "/api/v3/settings", async (req, _url, _ctx) => {
  const body = await readJsonBody(req);
  try {
    return json({ ok: true, settings: writeSettings(body) });
  } catch (e) {
    return json({ error: (e as Error).message }, { status: 400 });
  }
});

route("POST", "/api/v3/settings/favorite", async (req, _url, _ctx) => {
  const body = await readJsonBody(req);
  try {
    const favs = toggleFavorite(String(body.model ?? ""), Boolean(body.on));
    return json({ ok: true, favorites: favs });
  } catch (e) {
    return json({ error: (e as Error).message }, { status: 400 });
  }
});

route("GET", "/api/v3/cost-estimate", (_req, url, _ctx) => {
  const model = url.searchParams.get("model") ?? "gpt-4o";
  const dataset = url.searchParams.get("dataset") ?? "";
  const limit = Number(url.searchParams.get("limit") ?? 0) || 0;
  const maxTurns = Number(url.searchParams.get("max_turns") ?? 48) || 48;
  const info = modelInfo(model);
  let n = 20;
  try {
    const man = JSON.parse(nodeFs.readFileSync(dataset, "utf8"));
    n = limit > 0 ? limit : man.mazes.length;
  } catch {
    /* keep default */
  }
  const pin = Number(info.price_in ?? 0);
  const pout = Number(info.price_out ?? 0);
  const estIn = 260 * maxTurns;
  const estOut = 12;
  const cost = (n * maxTurns * (estIn * pin + estOut * pout)) / 1000;
  return json({
    model,
    mazes: n,
    turns: maxTurns,
    price_in: info.price_in ?? null,
    price_out: info.price_out ?? null,
    price_known: info.price_known ?? false,
    estimated_cost_usd: Math.round(cost * 10000) / 10000,
    note: "rough; response cache makes re-runs free",
  });
});

route("GET", "/api/v3/vision-probe", async (_req, _url, _ctx) => {
  const raster = await rasteriserStatus();
  return json({ rasteriser: raster });
});

route("GET", "/api/v3/response-cache", (_req, _url, ctx) =>
  json(responseCacheStats(ctx.resultsDir)),
);

route("POST", "/api/v3/run", async (req, _url, ctx) => {
  const body = await readJsonBody(req);
  const backend = String(body.backend ?? "");
  const model = String(body.model ?? "");
  const dataset = String(body.dataset ?? "");
  if (!backend || !model || !dataset) {
    return json({ error: "backend, model and dataset are required" }, { status: 400 });
  }
  if (!nodeFs.existsSync(dataset)) {
    return json({ error: `dataset not found: ${dataset}` }, { status: 400 });
  }
  try {
    const launched = ctx.launch({
      backend,
      model,
      dataset,
      mode: body.mode ? String(body.mode) : undefined,
      lag: body.lag ? Number(body.lag) : undefined,
      navigate: Boolean(body.navigate),
      limit: body.limit ? Number(body.limit) : 0,
      runId: body.run_id ? String(body.run_id) : undefined,
      runMode:
        body.run_mode === "test" || body.run_mode === "live"
          ? (body.run_mode as "live" | "test")
          : undefined,
      effort: body.effort ? String(body.effort) : undefined,
      maxTurns: body.max_turns ? Number(body.max_turns) : undefined,
      warmup: body.warmup !== false,
      framesDir: nodePath.join(ctx.resultsDir, "frames"),
    });
    return json({ ok: true, ...launched });
  } catch (e) {
    return json({ error: (e as Error).message }, { status: 400 });
  }
});

route("POST", "/api/v3/kill", async (req, _url, ctx) => {
  const body = await readJsonBody(req);
  const runId = String(body.run_id ?? "");
  if (!runId) return json({ error: "run_id required" }, { status: 400 });
  return json(killRun(ctx.resultsDir, runId));
});

route("GET", "/api/v3/live/:id", (req, _url, ctx, p) => {
  if (!req.headers.get("accept")?.includes("text/event-stream")) {
    return json({ error: "expected text/event-stream" }, { status: 400 });
  }
  const encoder = new TextEncoder();
  let heartbeat: ReturnType<typeof setInterval>;
  const stream = new ReadableStream({
    start(controller) {
      const send = (event: string, data: unknown): void => {
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      };
      send("hello", { run_id: p.id });
      const close = streamRun(ctx.resultsDir, p.id, send);
      heartbeat = setInterval(() => send("ping", { t: Date.now() }), 15000);
      req.signal.addEventListener("abort", () => {
        clearInterval(heartbeat);
        close();
        try {
          controller.close();
        } catch {
          /* already closed */
        }
      });
    },
    cancel() {
      clearInterval(heartbeat);
    },
  });
  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
});

route("GET", "/api/v3/frame", (_req, url, ctx) => {
  const h = url.searchParams.get("h") ?? "";
  if (!/^[0-9a-f]{16}$/.test(h)) return json({ error: "bad hash" }, { status: 400 });
  const p = nodePath.join(ctx.resultsDir, "frames", `${h}.png`);
  if (!nodeFs.existsSync(p)) {
    return json({ error: "frame not cached on this machine" }, { status: 404 });
  }
  return new Response(nodeFs.readFileSync(p), {
    headers: { "Content-Type": "image/png", "Cache-Control": "public, max-age=31536000" },
  });
});

route("GET", "/api/v3/integrity", (_req, _url, ctx) => {
  // Committed-artifact protection (v2's guard): which result files are
  // tracked by git. A live (real-model) run that is NOT tracked is at risk.
  const proc = Bun.spawnSync(["git", "-C", ctx.rootDir, "ls-files", "--", "results"]);
  const tracked = new Set(proc.stdout.toString().split("\n").filter(Boolean));
  const runs = enumerateRuns(ctx.resultsDir);
  const rows = Object.entries(runs).map(([rid, r]) => {
    const rel = nodePath.relative(ctx.rootDir, r.paths.summary ?? r.paths.jsonl);
    const isTracked =
      tracked.has(rel) || tracked.has(nodePath.relative(ctx.rootDir, r.paths.jsonl));
    return {
      run_id: rid,
      mode: r.summary?.mode ?? r.record?.mode ?? "?",
      tracked: isTracked,
      note: isTracked
        ? "committed"
        : "not committed — a live run should be committed to be auditable",
    };
  });
  return json({ runs: rows, git_available: proc.exitCode === 0 });
});

route("GET", "/api/v3/jobs", (_req, _url, _ctx) => json({ jobs: activeJobs() }));

// ------------------------------------------------------------------ util --

function loadBenchPropertiesSafe(ctx: BoardCtx): Rec | null {
  return loadBenchProperties(nodePath.join(ctx.resultsDir, "bench_properties.json"));
}

function relativize(paths: Record<string, string>, ctx: BoardCtx): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(paths)) {
    out[k] = nodePath.relative(ctx.rootDir, v);
  }
  return out;
}

export function handleApi(
  req: Request,
  url: URL,
  ctx: BoardCtx,
): Promise<Response> | Response | null {
  const path = url.pathname;
  for (const r of routes) {
    if (r.method !== req.method) continue;
    const m = path.match(r.pattern);
    if (!m) continue;
    const params: Record<string, string> = {};
    r.keys.forEach((k, i) => {
      params[k] = decodeURIComponent(m[i + 1]);
    });
    return r.handler(req, url, ctx, params);
  }
  if (path.startsWith("/api/")) {
    return json({ error: "no such route", path }, { status: 404 });
  }
  return null;
}
