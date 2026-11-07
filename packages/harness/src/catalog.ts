/**
 * The model catalogue: registry tables, provider specs, credentials,
 * discovery, token estimation — a port of v2's catalog.py.
 *
 * Data lives in registry/ at the repo root (providers.json, models.json);
 * a test enforces that these stay byte-identical to the frozen copies in
 * legacy/python-v2/drawtle/, so the two implementations can never disagree
 * about what a model costs or accepts. User-side state (overlay, keys)
 * lives in configs/local/ (gitignored; NEVER committed).
 */

import * as nodeFs from "node:fs";
import { existsSync, readFileSync } from "node:fs";
import * as nodePath from "node:path";

export const REPO_ROOT = nodePath.resolve(import.meta.dir, "../../..");
export const REGISTRY_DIR = nodePath.join(REPO_ROOT, "registry");
export const KNOWN_MODELS = nodePath.join(REGISTRY_DIR, "models.json");
export const KNOWN_PROVIDERS = nodePath.join(REGISTRY_DIR, "providers.json");
export const LOCAL_DIR = nodePath.join(REPO_ROOT, "configs", "local");
export const OVERLAY_FILE = nodePath.join(LOCAL_DIR, "overlay.json");
export const CRED_FILE = nodePath.join(LOCAL_DIR, "keys.json");
export const ENV_FILE = nodePath.join(REPO_ROOT, "configs", ".env");

export interface ProviderSpec {
  protocol: string;
  url: string | null;
  key_env: string[];
  models_url: string | null;
  models_path: string | null;
  model_provider?: string;
  auth: string;
  openai_compat: boolean;
  usage_fields: [string[], string[]] | null;
  context_note: string;
  self_hosted?: boolean;
  rate_limit: string | null;
  timeout_s?: number;
  request_params?: Record<string, unknown>;
  json_mode?: boolean;
}

export interface ModelInfo {
  provider?: string;
  context_window?: number;
  max_output?: number;
  price_in?: number;
  price_out?: number;
  price_known?: boolean;
  free_tier?: boolean;
  modalities?: string[];
  reasoning_effort_levels?: string[];
  source?: string;
  checked?: string;
  notes?: string;
  capabilities?: string[];
  [k: string]: unknown;
}

// ------------------------------------------------------------------ data ---

let _providers: Record<string, ProviderSpec> | null = null;

function loadProviders(): Record<string, ProviderSpec> {
  try {
    const blob = JSON.parse(readFileSync(KNOWN_PROVIDERS, "utf8"));
    return (blob.providers ?? {}) as Record<string, ProviderSpec>;
  } catch (e) {
    console.error(`warning: could not read providers.json (${String(e)})`);
    return {};
  }
}

export function providers(): Record<string, ProviderSpec> {
  if (_providers === null) {
    _providers = loadProviders();
  }
  return _providers;
}

export function providerSpec(kind: string): ProviderSpec | null {
  return providers()[kind] ?? null;
}

function loadOverlayModels(): Record<string, ModelInfo> {
  if (!existsSync(OVERLAY_FILE)) return {};
  try {
    const blob = JSON.parse(readFileSync(OVERLAY_FILE, "utf8"));
    return blob.models ?? {};
  } catch {
    return {};
  }
}

function readRegistryFile(): { models: Record<string, ModelInfo> } {
  if (!existsSync(KNOWN_MODELS)) return { models: {} };
  return JSON.parse(readFileSync(KNOWN_MODELS, "utf8"));
}

let _merged: Record<string, ModelInfo> | null = null;

export function mergedModels(): Record<string, ModelInfo> {
  if (_merged === null) {
    const base = readRegistryFile().models ?? {};
    const overlay = loadOverlayModels();
    _merged = { ...base, ...overlay };
  }
  return _merged;
}

export function invalidateModelCache(): void {
  _merged = null;
}

export function modelInfo(model: string): ModelInfo & {
  known: boolean;
  matched: string | null;
  id?: string;
} {
  const models = mergedModels();
  if (Object.prototype.hasOwnProperty.call(models, model)) {
    return { ...models[model], known: true, matched: model };
  }
  // longest prefix wins, so "gemini-2.5-flash" beats "gemini-2.5"
  let best: string | null = null;
  for (const name of Object.keys(models)) {
    if (model.startsWith(name) && (best === null || name.length > best.length)) {
      best = name;
    }
  }
  if (best) return { ...models[best], known: true, matched: best };
  return { id: model, known: false, matched: null };
}

export function contextWindow(model: string): number | undefined {
  return modelInfo(model).context_window;
}

export function maxOutput(model: string): number | undefined {
  return modelInfo(model).max_output;
}

/** (in_per_1k, out_per_1k) or null when unknown. Never a fabricated zero. */
export function priceOf(model: string): [number, number] | null {
  const info = modelInfo(model);
  const pin = info.price_in;
  const pout = info.price_out;
  if (pin === undefined || pin === null || pout === undefined || pout === null) {
    return null;
  }
  return [pin, pout];
}

/** Rough token estimate when a provider reports no usage. Images counted
 *  separately: a raster frame is not text. */
export function estimateRequestTokens(promptChars: number, imageCount = 0): number {
  const text = Math.max(1, Math.floor(promptChars / 4));
  return text + imageCount * 750;
}

const EFFORT_FIELD: Record<string, string> = {
  openai: "reasoning_effort",
  gemini: "reasoning_effort",
};

export function effortSupported(backend: string, model: string): [string | null, string[]] {
  if (!(backend === "openai" || backend === "gemini")) return [null, []];
  const levels = modelInfo(model).reasoning_effort_levels;
  if (!levels || levels.length === 0) return [null, []];
  return [EFFORT_FIELD[backend] ?? null, levels];
}

// --------------------------------------------------------------- keys -----

function loadDotenv(): void {
  if (!existsSync(ENV_FILE)) return;
  try {
    for (const line of readFileSync(ENV_FILE, "utf8").split(/\r?\n/)) {
      const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
      if (!m) continue;
      if (process.env[m[1]]) continue; // never override an exported var
      let v = m[2];
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
        v = v.slice(1, -1);
      }
      process.env[m[1]] = v;
    }
  } catch {
    /* unreadable .env is not fatal */
  }
}

function loadCredentialFile(): { keys: Record<string, string>; err: string | null } {
  if (!existsSync(CRED_FILE)) return { keys: {}, err: null };
  try {
    const data = JSON.parse(readFileSync(CRED_FILE, "utf8"));
    const keys = data.keys ?? {};
    if (typeof keys !== "object" || keys === null) {
      return { keys: {}, err: "credentials file has no 'keys' object" };
    }
    return { keys: keys as Record<string, string>, err: null };
  } catch (e) {
    return { keys: {}, err: `credentials file unreadable (${String(e)})` };
  }
}

function writeCredentialFile(keys: Record<string, string>): string {
  nodeFs.mkdirSync(nodePath.dirname(CRED_FILE), { recursive: true });
  const tmp = `${CRED_FILE}.${process.pid}.tmp`;
  const fh = nodeFs.openSync(tmp, "w");
  try {
    nodeFs.writeSync(fh, JSON.stringify({ keys }, null, 2));
    nodeFs.fsyncSync(fh);
  } finally {
    nodeFs.closeSync(fh);
  }
  nodeFs.renameSync(tmp, CRED_FILE);
  try {
    nodeFs.chmodSync(CRED_FILE, 0o600);
  } catch {
    /* Windows: ACLs, not mode bits */
  }
  return CRED_FILE;
}

export function storeKey(backend: string, key: string): string {
  const { keys } = loadCredentialFile();
  keys[String(backend).trim()] = key.trim();
  return writeCredentialFile(keys);
}

export function clearKey(backend: string): string {
  const b = String(backend ?? "").trim();
  const { keys } = loadCredentialFile();
  if (b in keys) {
    delete keys[b];
    writeCredentialFile(keys);
  }
  return CRED_FILE;
}

/** Precedence: explicit argument, environment, credential file. */
export function resolveKey(
  backend: string,
  explicit?: string | null,
): { key: string | null; source: string | null } {
  if (explicit) return { key: explicit, source: "argument" };
  loadDotenv();
  const spec = providers()[backend];
  for (const v of spec?.key_env ?? []) {
    const val = process.env[v];
    if (val) return { key: val, source: `env:${v}` };
  }
  const { keys } = loadCredentialFile();
  const v = keys[backend];
  if (v) return { key: v, source: "file" };
  return { key: null, source: null };
}

export function mask(key: string | null): string {
  if (!key) return "(none)";
  if (key.length <= 8) return "*".repeat(key.length);
  return `${key.slice(0, 4)}...${key.slice(-4)} (${key.length} chars)`;
}

// ------------------------------------------------------------- discovery --

function walkPath(obj: unknown, path: string | null): string[] {
  if (!path) return [];
  const m = path.match(/^([^[\]]*)\[\]\.(.*)$/);
  if (m) {
    // "data[].id" / "models[].name": walk the prefix, then map over the array
    let cur: unknown = obj;
    for (const p of m[1] ? m[1].split(".") : []) {
      cur = (cur as Record<string, unknown> | undefined | null)?.[p];
      if (cur === undefined || cur === null) return [];
    }
    if (!Array.isArray(cur)) return [];
    const rest = m[2];
    return cur
      .map((x) => (rest ? (x as Record<string, unknown>)?.[rest] : x))
      .filter((v): v is string => typeof v === "string");
  }
  let cur: unknown = obj;
  for (const p of path.split(".")) {
    cur = (cur as Record<string, unknown> | undefined | null)?.[p];
    if (cur === undefined || cur === null) return [];
  }
  return typeof cur === "string" ? [cur] : [];
}

export interface FetchModelsResult {
  ids: string[];
  meta: {
    ok: boolean;
    error?: string;
    source?: string | null;
    env?: string[];
    url?: string | null;
    n?: number;
  };
}

/** Ask the provider which models exist. Only ids are taken; pricing and
 *  windows are NOT on these endpoints. */
export async function fetchModels(
  backend: string,
  key?: string | null,
): Promise<FetchModelsResult> {
  const spec = providers()[backend];
  if (!spec?.models_url) {
    return { ids: [], meta: { ok: false, error: `no discovery endpoint for '${backend}'` } };
  }
  const { key: k, source } = resolveKey(backend, key ?? null);
  if (!k) {
    return {
      ids: [],
      meta: { ok: false, error: "no key available", env: spec.key_env, source: null },
    };
  }
  let url = spec.models_url;
  const headers: Record<string, string> = {};
  if (spec.auth === "bearer") headers.Authorization = `Bearer ${k}`;
  else if (spec.auth === "x-api-key") {
    headers["x-api-key"] = k;
    headers["anthropic-version"] = "2023-06-01";
  } else if (spec.auth === "query") {
    url = `${url}${url.includes("?") ? "&" : "?"}key=${encodeURIComponent(k)}`;
  }
  try {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), 20_000);
    const res = await fetch(url, { headers, signal: ac.signal });
    clearTimeout(timer);
    if (!res.ok) {
      const hint: Record<number, string> = {
        401: "key rejected",
        403: "key lacks permission for this route",
        404: "discovery route not found",
      };
      return {
        ids: [],
        meta: { ok: false, error: hint[res.status] ?? `HTTP ${res.status}`, source },
      };
    }
    const payload = await res.json();
    const ids = walkPath(payload, spec.models_path);
    return { ids, meta: { ok: true, source, n: ids.length, url: spec.models_url } };
  } catch (e) {
    return {
      ids: [],
      meta: { ok: false, error: `${(e as Error).name} (${spec.models_url}) failed`, source },
    };
  }
}
