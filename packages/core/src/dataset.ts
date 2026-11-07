/**
 * Versioned, reproducible maze dataset — a byte-exact port of
 * legacy/python-v2/drawtle/dataset.py.
 *
 * Every maze has a seed, size, and exit-pair recorded. Manifests are written
 * byte-identically to v2 (`json.dumps(..., indent=2)`), and hashed with
 * `json.dumps(bare, sort_keys=True)` (spaced separators) so a run is fully
 * reproducible and a result can be audited.
 *
 * Maze seeds are 64-bit integers (up to 2^64-1) — beyond ECMAScript's safe
 * integer range. They are held as BigInt and written as raw decimal digits;
 * the reader parses them losslessly by re-quoting "seed": <digits> before
 * JSON.parse. See fixtures/golden/mazes.json.
 */

import { createHash } from "node:crypto";
import { PyRandom } from "./py";
import { make, type Maze, EXIT_PAIRS } from "./maze";

export const DATASET_VERSION = "1";
export const SIZES: readonly number[] = [9, 11, 13];
export const PAIRS: readonly string[] = Object.keys(EXIT_PAIRS); // NW, WS, SE, EN
export const DATASET_SEED = 20260918;

export interface MazeSpec {
  idx: number;
  size: number;
  pair: string;
  seed: bigint;
}

export interface Manifest {
  version: string;
  count: number;
  sizes: number[];
  pairs: string[];
  seed: number;
  mazes: MazeSpec[];
  hash: string;
}

/** A stable, collision-resistant seed per maze index. */
function mazeSeed(runSeed: number, idx: number): bigint {
  const h = createHash("sha256")
    .update(`${runSeed}:${idx}`)
    .digest("hex");
  return BigInt(`0x${h.slice(0, 16)}`);
}

export function buildManifest(
  count = 200,
  sizes: readonly number[] = SIZES,
  pairs: readonly string[] = PAIRS,
  seed = DATASET_SEED
): Manifest {
  const rng = new PyRandom(seed);
  const items: MazeSpec[] = [];
  for (let i = 0; i < count; i++) {
    const size = rng.choice(sizes as readonly number[]);
    const pair = rng.choice(pairs);
    const ms = mazeSeed(seed, i);
    items.push({ idx: i, size, pair, seed: ms });
  }
  const man: Manifest = {
    version: DATASET_VERSION,
    count,
    sizes: [...sizes],
    pairs: [...pairs],
    seed,
    mazes: items,
    hash: "",
  };
  man.hash = hashManifest(man);
  return man;
}

export const DATASET_PRESETS: readonly [string, number, string][] = [
  ["dataset-200.json", 200, "full"],
  ["dataset-100.json", 100, "half"],
  ["dataset-50.json", 50, "fifty"],
  ["dataset-20.json", 20, "mini"],
];

// ----------------------------------------------------------- serialization --

type J =
  | null
  | boolean
  | number
  | bigint
  | string
  | J[]
  | { [k: string]: J };

function escStr(s: string): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 0x22) out += '\\"';
    else if (c === 0x5c) out += "\\\\";
    else if (c === 0x0a) out += "\\n";
    else if (c === 0x0d) out += "\\r";
    else if (c === 0x09) out += "\\t";
    else if (c === 0x08) out += "\\b";
    else if (c === 0x0c) out += "\\f";
    else if (c < 0x20 || c > 0x7e) {
      out += `\\u${c.toString(16).padStart(4, "0")}`;
    } else out += s[i];
  }
  return out + '"';
}

function scalar(x: J): string {
  if (x === null) return "null";
  if (typeof x === "boolean") return x ? "true" : "false";
  if (typeof x === "bigint") return x.toString();
  if (typeof x === "number") {
    if (Number.isInteger(x) && Math.abs(x) < 1e16) return x.toString();
    // floats: Python repr — our manifests carry only ints, but be exact.
    return x.toString();
  }
  return escStr(x);
}

function isPlainObject(x: J): x is { [k: string]: J } {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

/** json.dumps(x, indent=2) — insertion key order, ', '/': ' separators. */
function dumpsIndent2(x: J, indent = 0): string {
  const pad = "  ".repeat(indent);
  const padIn = "  ".repeat(indent + 1);
  if (Array.isArray(x)) {
    if (x.length === 0) return "[]";
    const items = x
      .map((v) => padIn + dumpsIndent2(v, indent + 1))
      .join(",\n");
    return `[\n${items}\n${pad}]`;
  }
  if (isPlainObject(x)) {
    const keys = Object.keys(x);
    if (keys.length === 0) return "{}";
    const items = keys
      .map((k) => `${padIn}${escStr(k)}: ${dumpsIndent2(x[k] as J, indent + 1)}`)
      .join(",\n");
    return `{\n${items}\n${pad}}`;
  }
  return scalar(x);
}

/** json.dumps(x, sort_keys=True) — single line, ', '/': ' separators. */
function dumpsSortedSpaced(x: J): string {
  if (Array.isArray(x)) {
    return "[" + x.map((v) => dumpsSortedSpaced(v)).join(", ") + "]";
  }
  if (isPlainObject(x)) {
    const keys = Object.keys(x).sort();
    return (
      "{" +
      keys.map((k) => `${escStr(k)}: ${dumpsSortedSpaced(x[k] as J)}`).join(", ") +
      "}"
    );
  }
  return scalar(x);
}

/** sha256 of the canonical sorted dump, truncated to 16 hex chars. */
export function hashManifest(man: Manifest): string {
  const bare: J = {
    version: man.version,
    count: man.count,
    sizes: man.sizes,
    pairs: man.pairs,
    seed: man.seed,
    mazes: man.mazes.map((m) => ({
      idx: m.idx,
      size: m.size,
      pair: m.pair,
      seed: m.seed,
    })),
  };
  const blob = dumpsSortedSpaced(bare);
  const h = createHash("sha256").update(blob, "utf8").digest("hex");
  return `sha256:${h.slice(0, 16)}`;
}

/** The exact file text v2's save_manifest wrote (no trailing newline). */
export function manifestText(man: Manifest): string {
  return dumpsIndent2({
    version: man.version,
    count: man.count,
    sizes: man.sizes,
    pairs: man.pairs,
    seed: man.seed,
    mazes: man.mazes.map((m) => ({
      idx: m.idx,
      size: m.size,
      pair: m.pair,
      seed: m.seed,
    })),
    hash: man.hash,
  });
}

// ------------------------------------------------------------------ IO ----

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import * as nodePath from "node:path";

export function saveManifest(man: Manifest, path: string): string {
  const parent = nodePath.dirname(nodePath.resolve(path));
  mkdirSync(parent, { recursive: true });
  writeFileSync(path, manifestText(man), "utf8");
  return path;
}

/**
 * Lossless manifest reader. JSON.parse cannot hold seeds beyond 2^53, so
 * every `"seed": <digits>` occurrence is re-quoted to a string first and
 * converted back to BigInt by the reviver.
 */
export function loadManifest(path: string): Manifest {
  const text = readFileSync(path, "utf8");
  return parseManifestText(text);
}

export function parseManifestText(text: string): Manifest {
  const requoted = text.replace(/"seed":\s*(\d+)/g, '"seed": "$1"');
  const raw = JSON.parse(requoted, (key, value) => {
    if (key === "seed" && typeof value === "string" && /^\d+$/.test(value)) {
      return BigInt(value);
    }
    return value;
  }) as unknown as {
    version: string;
    count: number;
    sizes: number[];
    pairs: string[];
    seed: number;
    mazes: Array<{ idx: number; size: number; pair: string; seed: bigint }>;
    hash: string;
  };
  if (typeof raw.seed === "string") {
    // top-level run seed got the same treatment; it is small, keep as number
    raw.seed = Number(raw.seed as unknown as string);
  }
  return raw as Manifest;
}

/** Materialise one maze object from a manifest item. */
export function makeMaze(spec: MazeSpec): Maze {
  const rng = new PyRandom(spec.seed);
  return make(spec.size, spec.size, spec.pair, rng);
}

export function writePresetManifests(
  outDir: string,
  seed = DATASET_SEED,
  sizes: readonly number[] = SIZES
): Array<{ path: string; count: number; label: string; hash: string }> {
  const written: Array<{ path: string; count: number; label: string; hash: string }> = [];
  for (const [name, count, label] of DATASET_PRESETS) {
    const man = buildManifest(count, sizes, PAIRS, seed);
    const path = saveManifest(man, nodePath.join(outDir, name));
    written.push({ path, count, label, hash: man.hash });
  }
  return written;
}
