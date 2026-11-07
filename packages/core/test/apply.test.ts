import { describe, expect, test } from "bun:test";
import { type MazeSpec, makeMaze } from "../src/dataset";
import { applyAction, distanceField, initialHeading, rotateWalls } from "../src/maze";
import { progressScore } from "../src/protocol";
import { pyRoundInt } from "../src/py";
import { loadJson } from "./helpers";

const fix = loadJson<{
  cases: Array<{
    idx: number;
    size: number;
    rot: number;
    turn: number;
    step: number;
    off_lattice: boolean;
    applied_cell: number[];
    heading_applied: number;
    progressed: boolean;
    hit_wall: boolean;
  }>;
}>("apply_progress.json");

const mazesFix = loadJson<{
  mazes: Array<{ idx: number; size: number; pair: string; seed: string }>;
}>("mazes.json");

const specOf = (idx: number): MazeSpec => {
  const m = mazesFix.mazes.find((x) => x.idx === idx)!;
  return { idx: m.idx, size: m.size, pair: m.pair, seed: BigInt(m.seed) };
};

describe("apply/progress parity (240 cases across 3 mazes x 4 rotations)", () => {
  test("off-lattice detection, application, scoring all match v2", () => {
    const byIdx = new Map<number, ReturnType<typeof makeMaze>>();
    for (const c of fix.cases) {
      let base = byIdx.get(c.idx);
      if (!base) {
        base = makeMaze(specOf(c.idx));
        byIdx.set(c.idx, base);
      }
      const m2 = rotateWalls(base, c.rot);
      const dist = distanceField(m2);
      const h0 = initialHeading(base);
      const action = [c.turn, c.step] as const;

      // the runner's off-lattice rule, verbatim
      const raw = (((h0 + c.turn) % 360) + 360) % 360;
      const snap = (((pyRoundInt(raw / 90.0) * 90) % 360) + 360) % 360;
      const off = Math.abs(raw - snap) > 1e-6;
      expect(off).toBe(c.off_lattice);

      const [ncell, nhead] = applyAction(m2, base.entry, h0, action);
      expect(ncell[0]).toBe(c.applied_cell[0]);
      expect(ncell[1]).toBe(c.applied_cell[1]);
      expect(nhead).toBe(c.heading_applied);

      const prog = progressScore(m2, base.entry, h0, dist, action);
      expect(prog).toBe(c.progressed);

      const hit = action !== null && ncell === base.entry;
      expect(hit).toBe(c.hit_wall);
    }
    expect(fix.cases.length).toBe(240);
  });
});
