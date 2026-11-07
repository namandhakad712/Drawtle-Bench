/**
 * User settings for the control centre — a port of v2's settings.py.
 * Stored in configs/local/settings.json (gitignored): a setting changed in
 * the panel must not dirty the working tree.
 *
 * Rules carried over: unknown keys are rejected; every value is validated
 * on the way in AND on the way out.
 */

import * as nodeFs from "node:fs";
import * as nodePath from "node:path";
import { LOCAL_DIR } from "./catalog";

export const SETTINGS_FILE =
  process.env.DRAWTLE_SETTINGS_FILE ?? nodePath.join(LOCAL_DIR, "settings.json");

export const DEFAULTS: Record<string, unknown> = {
  theme: "light",
  log_follow: true,
  test_mode: false,
  retention_days: 0,
  allow_docker_start: false,
  probe_timeout_s: 20.0,
  default_backend: "",
  default_model: "",
  default_dataset: "",
  favorites: [] as string[],
};

const THEMES = ["light", "dark"] as const;
const BOUNDS: Record<string, [number, number]> = {
  retention_days: [0, 3650],
  probe_timeout_s: [1.0, 300.0],
};

export class SettingsError extends Error {}
export class SettingsWriteError extends SettingsError {}

function coerce(key: string, value: unknown): unknown {
  const def = DEFAULTS[key];
  if (Array.isArray(def)) {
    if (!Array.isArray(value)) throw new SettingsError(`${key} must be a list`);
    const out: string[] = [];
    for (const item of value) {
      const s = String(item).trim();
      if (s && !out.includes(s)) out.push(s);
    }
    return out.slice(0, 500);
  }
  if (typeof def === "boolean") {
    if (typeof value === "boolean") return value;
    if (typeof value === "string" && ["true", "false"].includes(value.toLowerCase())) {
      return value.toLowerCase() === "true";
    }
    throw new SettingsError(`${key} must be true or false`);
  }
  if (typeof def === "number") {
    const n = Number(value);
    if (!Number.isFinite(n)) throw new SettingsError(`${key} must be a number`);
    const [lo, hi] = BOUNDS[key] ?? [0, 1e9];
    return Math.max(lo, Math.min(hi, n));
  }
  const s = value === null ? "" : String(value);
  if (key === "theme" && !THEMES.includes(s as (typeof THEMES)[number])) {
    throw new SettingsError(`theme must be one of ${THEMES.join(", ")}`);
  }
  return s;
}

export function read(): Record<string, unknown> {
  const out = { ...DEFAULTS };
  try {
    const blob = JSON.parse(nodeFs.readFileSync(SETTINGS_FILE, "utf8"));
    if (typeof blob !== "object" || blob === null) return out;
    for (const key of Object.keys(DEFAULTS)) {
      if (!(key in blob)) continue;
      try {
        out[key] = coerce(key, blob[key]);
      } catch {
        /* keep the default for this one key */
      }
    }
  } catch {
    return out;
  }
  return out;
}

export function write(patch: Record<string, unknown>): Record<string, unknown> {
  if (typeof patch !== "object" || patch === null) {
    throw new SettingsError("settings payload must be an object");
  }
  const unknown = Object.keys(patch).filter((k) => !(k in DEFAULTS));
  if (unknown.length) {
    throw new SettingsError(
      `unknown setting(s): ${unknown.join(", ")}. Known settings: ${Object.keys(DEFAULTS).join(", ")}`,
    );
  }
  const current = read();
  for (const [key, value] of Object.entries(patch)) {
    current[key] = coerce(key, value);
  }
  current.updated = new Date()
    .toISOString()
    .replace(/\.\d+Z$/, "Z")
    .replace("T", "T");
  try {
    nodeFs.mkdirSync(nodePath.dirname(SETTINGS_FILE), { recursive: true });
    const tmp = `${SETTINGS_FILE}.${process.pid}.tmp`;
    nodeFs.writeFileSync(tmp, JSON.stringify(current, null, 2));
    nodeFs.renameSync(tmp, SETTINGS_FILE);
  } catch (e) {
    throw new SettingsWriteError(
      `could not write ${SETTINGS_FILE} (${String(e)}); the config directory may be read-only`,
    );
  }
  return current;
}

export function describe(): Record<string, unknown> {
  return {
    settings: read(),
    path: SETTINGS_FILE,
    exists: nodeFs.existsSync(SETTINGS_FILE),
    defaults: { ...DEFAULTS },
  };
}

export function toggleFavorite(mid: string, on: boolean): string[] {
  const id = String(mid ?? "").trim();
  if (!id) throw new SettingsError("a model id is required");
  const favs = [...(((read().favorites as string[]) ?? []) as string[])];
  const onB =
    typeof on === "boolean" ? on : ["true", "1", "yes", "on"].includes(String(on).toLowerCase());
  if (onB && !favs.includes(id)) favs.push(id);
  else if (!onB && favs.includes(id)) favs.splice(favs.indexOf(id), 1);
  return (write({ favorites: favs }).favorites as string[]) ?? [];
}
