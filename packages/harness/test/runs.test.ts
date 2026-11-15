import { describe, expect, test } from "bun:test";
import * as nodePath from "node:path";
import { RESULTS, expectDeepEqual, expectSupersetEqual, loadJson } from "../../core/test/helpers";
import { enumerateRuns, excludedRuns, leaderboard } from "../src/stats";

const fix = loadJson<{
  runs: Record<
    string,
    {
      summary: Record<string, unknown> | null;
      record: Record<string, unknown>;
      path: string | null;
      paths: Record<string, string>;
      layout: "dir" | "flat";
      status: string;
      status_source: string;
      note: string | null;
    }
  >;
  leaderboard: Array<Record<string, unknown>>;
  leaderboard_test: Array<Record<string, unknown>>;
  leaderboard_live: Array<Record<string, unknown>>;
  excluded: Array<Record<string, unknown>>;
}>("runs_enumeration.json");

function relativize(r: Record<string, unknown>): Record<string, unknown> {
  const { statusSource, ...rest } = r as Record<string, unknown> & {
    statusSource?: string;
  };
  const out: Record<string, unknown> = { ...rest };
  if (typeof out.paths === "object" && out.paths !== null) {
    const paths: Record<string, string> = {};
    for (const [k, v] of Object.entries(out.paths as Record<string, string>)) {
      paths[k] = nodePath.relative(RESULTS, v);
    }
    out.paths = paths;
  }
  if (typeof out.path === "string" && out.path.startsWith("/")) {
    out.path = nodePath.relative(RESULTS, out.path);
  }
  if (statusSource !== undefined) {
    out.status_source = statusSource;
  }
  return out;
}

describe("results-tree enumeration parity (v2 + v3 layouts, all runs)", () => {
  // Parity contract (same as stats.test.ts): every field the frozen v2
  // produced must be reproduced bit-exactly; v3 may ADD fields under new
  // names (v3.1: paths.events, level_selector, levels_hash, by_level) —
  // those are asserted in the v3-extras test below, not dropped from the
  // comparison by weakening this one.
  test("enumerateRuns reproduces v2 exactly", () => {
    const got = enumerateRuns(RESULTS);
    const gotRel: Record<string, unknown> = {};
    for (const [rid, entry] of Object.entries(got)) {
      gotRel[rid] = relativize(entry as unknown as Record<string, unknown>);
    }
    expectSupersetEqual(gotRel, fix.runs, "runs");
    for (const entry of Object.values(got)) {
      expect(entry.paths.events).toBeTruthy(); // v3.1: the event log has a path
    }
  });

  test("leaderboard reproduces v2 exactly (clean runs)", () => {
    const rows = leaderboard(RESULTS, { onlyClean: true });
    expectSupersetEqual(rows, fix.leaderboard, "lb");
    // Level metadata is per-run reality, not a constant: legacy runs
    // predate levels (agnes: null); runs that attached levels carry their
    // selector and a per-level breakdown (the v3.3 zai runs: "all").
    for (const r of rows) {
      if (String(r.run_id).includes("agnes")) {
        expect(r.level_selector).toBe(null);
        expect(r.by_level).toBe(null);
      } else {
        expect(r.level_selector).toBe("all");
        expect(Array.isArray(r.by_level)).toBe(true);
      }
    }
  });

  test("mode filters: test ranks mocks only, live ranks real runs only", () => {
    expectSupersetEqual(
      leaderboard(RESULTS, { onlyClean: true, mode: "test" }),
      fix.leaderboard_test,
      "lb-test",
    );
    expectSupersetEqual(
      leaderboard(RESULTS, { onlyClean: true, mode: "live" }),
      fix.leaderboard_live,
      "lb-live",
    );
  });

  test("v3.1: --level filter rejects bad selectors and matches measured levels", () => {
    expect(() => leaderboard(RESULTS, { level: "L5" })).toThrow(/invalid --level/);
    expect(() => leaderboard(RESULTS, { level: "nope" })).toThrow(/invalid --level/);
    // The completed zai run attached levels (selector "all"), so slices
    // match it; agnes predates levels and the in-flight twin has no summary
    // yet, so neither matches a slice.
    expect(leaderboard(RESULTS, { level: "diamond" }).map((r) => r.run_id)).toEqual([
      "run-glm-5.3-flash-vision",
    ]);
    expect(leaderboard(RESULTS, { level: "L4" }).length).toBe(1);
    expect(leaderboard(RESULTS, { level: "all" }).length).toBe(
      leaderboard(RESULTS, { onlyClean: true }).length,
    );
  });

  test("excluded runs reproduce v2 exactly (no silent omissions)", () => {
    expectDeepEqual(excludedRuns(RESULTS), fix.excluded, "excluded");
  });

  test("live ranking: the vision run reads frames; the in-flight twin is parked, not ranked", () => {
    const live = leaderboard(RESULTS, { onlyClean: true, mode: "live" });
    expect(live.length).toBe(2);
    // Ranked by progress: glm-5.3-flash WITH frames (0.243) > agnes
    // (0.169). The blind text twin is mid-run (status error, resumable)
    // and therefore excluded — an unfinished run must not rank.
    expect(live[0].run_id).toBe("run-glm-5.3-flash-vision");
    expect(live[0].status).toBe("success");
    expect(live[1].run_id).toContain("agnes");
    const ex = excludedRuns(RESULTS);
    // two flat legacy mocks (unknown) + the resumable twin (error)
    const byStatus = ex.map((e) => String(e.status)).sort();
    expect(byStatus).toEqual(["error", "unknown", "unknown"]);
    for (const e of ex) {
      expect(e.reason).toBeTruthy();
    }
    expect(ex.length).toBe(3);
    const twin = ex.find((e) => String(e.run_id).includes("glm-4-plus"));
    expect(twin?.status).toBe("error"); // crashed runs resume, they don't rank
  });
});
