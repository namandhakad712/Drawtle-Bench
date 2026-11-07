import * as nodePath from "node:path";
import { readFileSync } from "node:fs";

export const REPO_ROOT = nodePath.resolve(import.meta.dir, "../../..");
export const GOLDEN = nodePath.join(REPO_ROOT, "fixtures", "golden");
export const RESULTS = nodePath.join(REPO_ROOT, "results");

export function loadJson<T = unknown>(name: string): T {
  return JSON.parse(readFileSync(nodePath.join(GOLDEN, name), "utf8")) as T;
}

export function loadGoldenText(rel: string): string {
  return readFileSync(nodePath.join(GOLDEN, rel), "utf8");
}

export function loadResultsText(rel: string): string {
  return readFileSync(nodePath.join(RESULTS, rel), "utf8");
}

/** Structural deep-equality for fixture comparison: treats 1 == 1.0,
 *  compares arrays by length+elements, objects by key sets. */
import { expect } from "bun:test";

export function expectDeepEqual(actual: unknown, expected: unknown, at = ""): void {
  if (Array.isArray(expected)) {
    expect(Array.isArray(actual)).toBeTrue();
    expect((actual as unknown[]).length).toBe(expected.length);
    for (let i = 0; i < expected.length; i++) {
      expectDeepEqual((actual as unknown[])[i], expected[i], `${at}[${i}]`);
    }
    return;
  }
  if (expected !== null && typeof expected === "object") {
    expect(actual).not.toBeNull();
    expect(typeof actual).toBe("object");
    expect(Array.isArray(actual)).toBeFalse();
    const ea = actual as Record<string, unknown>;
    for (const k of Object.keys(expected as Record<string, unknown>)) {
      expect(k in ea).toBeTrue(`${at}: missing key ${k}`);
      expectDeepEqual(ea[k], (expected as Record<string, unknown>)[k], `${at}.${k}`);
    }
    for (const k of Object.keys(ea)) {
      expect(k in (expected as Record<string, unknown>)).toBeTrue(
        `${at}: extra key ${k}`
      );
    }
    return;
  }
  if (typeof expected === "number" && typeof actual === "number") {
    expect(actual).toBe(expected); // bit-exact; 1 === 1.0 in ECMAScript
    return;
  }
  expect(actual).toBe(expected);
}
