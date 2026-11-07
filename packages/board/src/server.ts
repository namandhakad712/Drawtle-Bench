/**
 * The v3 control centre server: Bun.serve with the JSON API, an SSE live
 * stream, and a zero-build client — Preact + htm via importmap, TSX
 * transpiled on request. No bundler, no build step, no CDN dependency.
 *
 * Security: binds 127.0.0.1 by default (a control centre with a launch
 * button is not for the public internet). POST endpoints validate their
 * inputs; everything rendered is escaped by Preact; no innerHTML anywhere.
 */

import * as nodeFs from "node:fs";
import * as nodePath from "node:path";
import { handleApi, type BoardCtx } from "./api";
import { launchRun } from "./supervisor";

const HERE = nodePath.dirname(nodePath.resolve(import.meta.dir));
// HERE = packages/board (import.meta.dir is .../board/src).
const BOARD_ROOT = HERE;
const REPO_ROOT = nodePath.resolve(BOARD_ROOT, "../..");

const transpiler = new Bun.Transpiler({
  loader: "tsx",
  target: "browser",
});

const moduleCache = new Map<string, { code: string; mtime: number }>();

function serveModule(path: string): Response | null {
  try {
    const stat = nodeFs.statSync(path);
    const hit = moduleCache.get(path);
    if (hit && hit.mtime === stat.mtimeMs) {
      return new Response(hit.code, {
        headers: { "Content-Type": "text/javascript; charset=utf-8" },
      });
    }
    const src = nodeFs.readFileSync(path, "utf8");
    const code = transpiler.transformSync(src);
    moduleCache.set(path, { code, mtime: stat.mtimeMs });
    return new Response(code, {
      headers: { "Content-Type": "text/javascript; charset=utf-8" },
    });
  } catch {
    return null;
  }
}

function serveFile(path: string, type: string, cache?: string): Response | null {
  try {
    const body = nodeFs.readFileSync(path);
    const headers: Record<string, string> = { "Content-Type": type };
    if (cache) headers["Cache-Control"] = cache;
    return new Response(body, { headers });
  } catch {
    return null;
  }
}

/** Vendor files for the importmap, served straight from node_modules. */
const VENDOR: Record<string, string> = {
  "/vendor/preact.js": "preact/dist/preact.module.js",
  "/vendor/preact-hooks.js": "preact/hooks/dist/hooks.module.js",
  "/vendor/preact-devtools.js": "preact/devtools/dist/devtools.module.js",
  "/vendor/htm.js": "htm/dist/htm.module.js",
};

export interface BoardOpts {
  port?: number;
  host?: string;
  resultsDir?: string;
  open?: boolean;
  token?: string;
}

export function startBoard(opts: BoardOpts = {}): void {
  const port = opts.port ?? 8080;
  const host = opts.host ?? "127.0.0.1";
  const resultsDir = nodePath.resolve(opts.resultsDir ?? nodePath.join(REPO_ROOT, "results"));
  const cliPath = nodePath.join(REPO_ROOT, "packages", "cli", "src", "index.ts");
  const ctx: BoardCtx = {
    resultsDir,
    rootDir: REPO_ROOT,
    cliPath,
    launch: (spec) => launchRun(cliPath, resultsDir, spec as never),
  };
  const token = opts.token ?? process.env.DRAWTLE_BOARD_TOKEN ?? null;

  const server = Bun.serve({
    port,
    hostname: host,
    async fetch(req): Promise<Response> {
      const url = new URL(req.url);
      const path = url.pathname;

      // Optional token gate for anything that is not the shell itself.
      if (token && path !== "/" && path !== "/theme.css") {
        const supplied =
          req.headers.get("x-board-token") ??
          url.searchParams.get("token") ??
          req.headers.get("cookie")?.match(/board_token=([^;]+)/)?.[1];
        if (supplied !== token) {
          return new Response("unauthorized", { status: 401 });
        }
      }

      const api = handleApi(req, url, ctx);
      if (api) return api;

      if (path === "/" || path === "/index.html") {
        return (
          serveFile(nodePath.join(BOARD_ROOT, "public", "index.html"), "text/html; charset=utf-8") ??
          new Response("missing index.html", { status: 500 })
        );
      }
      if (path === "/theme.css") {
        return (
          serveFile(nodePath.join(BOARD_ROOT, "public", "theme.css"), "text/css; charset=utf-8", "no-cache") ??
          new Response("missing theme.css", { status: 500 })
        );
      }
      if (path.startsWith("/app/")) {
        const rel = path.slice("/app/".length);
        if (rel.includes("..")) return new Response("bad path", { status: 400 });
        const mod =
          serveModule(nodePath.join(BOARD_ROOT, "app", rel)) ??
          serveModule(nodePath.join(BOARD_ROOT, "app", `${rel}.tsx`)) ??
          serveModule(nodePath.join(BOARD_ROOT, "app", `${rel}.ts`));
        if (mod) return mod;
      }
      const vendorTarget = VENDOR[path];
      if (vendorTarget) {
        for (const root of ["node_modules", "../../node_modules"]) {
          const p = nodePath.resolve(BOARD_ROOT, root, vendorTarget);
          const res = serveFile(p, "text/javascript; charset=utf-8", "public, max-age=86400");
          if (res) return res;
        }
      }
      if (path === "/favicon.svg") {
        return (
          serveFile(nodePath.join(BOARD_ROOT, "public", "favicon.svg"), "image/svg+xml") ??
          new Response("", { status: 204 })
        );
      }
      return new Response("not found", { status: 404 });
    },
  });

  const url = `http://${server.hostname}:${server.port}/`;
  console.log(`drawtle control centre v3 — ${url}`);
  console.log(`  results: ${resultsDir}`);
  console.log(`  bound to ${server.hostname} (local only; use DRAWTLE_BOARD_TOKEN to add a token gate)`);
  if (opts.open !== false) {
    const opener =
      process.platform === "darwin" ? "open" : process.platform === "win32" ? "start" : "xdg-open";
    try {
      Bun.spawn([opener, url], { stdout: "ignore", stderr: "ignore" });
    } catch {
      console.log(`  open ${url} in a browser`);
    }
  }
}

// Direct entry: `bun packages/board/src/server.ts [--port N] [--host H]
// [--dir RESULTS] [--no-open]`
if (import.meta.main) {
  const argv = process.argv.slice(2);
  const get = (name: string, d: string): string => {
    const i = argv.indexOf(`--${name}`);
    if (i >= 0 && argv[i + 1]) return argv[i + 1];
    return d;
  };
  startBoard({
    port: Number(get("port", "8080")),
    host: get("host", "127.0.0.1"),
    resultsDir: get("dir", undefined as unknown as string),
    open: !argv.includes("--no-open"),
  });
}
