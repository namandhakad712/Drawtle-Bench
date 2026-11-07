import { describe, expect, test } from "bun:test";
import * as nodePath from "node:path";
import { loadJson, RESULTS, expectDeepEqual } from "../../core/test/helpers";
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
  const out = { ...r };
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
  const entry = r as { statusSource?: string };
  if (entry.statusSource !== undefined) {
    delete (out as Record<string, unknown>).statusSource;
    out.status_source = entry.statusSource;
  }
  return out;
}

describe("results-tree enumeration parity (v2 + v3 layouts, all runs)", () => {
  test("enumerateRuns reproduces v2 exactly", () => {
    const got = enumerateRuns(RESULTS);
    const gotRel: Record<string, unknown> = {};
    for (const [rid, entry] of Object.entries(got)) {
      gotRel[rid] = relativize(entry as unknown as Record<string, unknown>);
    }
    expectDeepEqual(gotRel, fix.runs, "runs");
  });

  test("leaderboard reproduces v2 exactly (clean runs)", () => {
    expectDeepEqual(leaderboard(RESULTS, { onlyClean: true }), fix.leaderboard, "lb");
  });

  test("mode filters: test ranks mocks only, live ranks real runs only", () => {
    expectDeepEqual(
      leaderboard(RESULTS, { onlyClean: true, mode: "test" }),
      fix.leaderboard_test,
      "lb-test"
    );
    expectDeepEqual(
      leaderboard(RESULTS, { onlyClean: true, mode: "live" }),
      fix.leaderboard_live,
      "lb-live"
    );
  });

  test("excluded runs reproduce v2 exactly (no silent omissions)", () => {
    expectDeepEqual(excludedRuns(RESULTS), fix.excluded, "excluded");
  });

  test("the agnes run ranks live; legacy mocks are excluded, not phantom-dropped", () => {
    const live = leaderboard(RESULTS, { onlyClean: true, mode: "live" });
    expect(live.length).toBeGreaterThan(0);
    expect(live[0].run_id).toContain("agnes");
    expect(live[0].status).toBe("success");
    const ex = excludedRuns(RESULTS);
    for (const e of ex) {
      expect(e.status).toBe("unknown"); // the two flat legacy mocks, no status file
      expect(e.reason).toBeTruthy();
    }
    expect(ex.length).toBe(2);
  });
});
