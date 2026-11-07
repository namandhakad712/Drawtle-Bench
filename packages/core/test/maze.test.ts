import { describe, expect, test } from "bun:test";
import { PyRandom } from "../src/py";
import { pyFloatStr } from "../src/py";
import {
  make,
  initialHeading,
  distanceField,
  optimalAction,
  applyAction,
  solve,
  rotateWalls,
  EXIT_PAIRS,
  type Cell,
} from "../src/maze";
import { makeMaze, type MazeSpec } from "../src/dataset";
import { loadJson } from "./helpers";

const mazesFix = loadJson<{
  count: number;
  mazes: Array<{
    idx: number;
    size: number;
    pair: string;
    seed: string;
    entry: number[];
    exits: number[][];
    initial_heading: number;
    blocked: string[];
    dist: number[][];
    dist_entry: number;
    optimal_entry: { turn: number; step: number } | null;
    solve: number[][];
    solve_final_cell: number[];
    walls: string[];
  }>;
}>("mazes.json");

const refFix = loadJson<{
  bases: Array<{
    seed_index: number;
    rotations: number[];
    size: number;
    pair: string;
    entry: number[];
    exits: number[][];
    initial_heading: number;
    blocked: string[];
    dist: number[][];
    dist_entry: number;
    optimal_entry: { turn: number; step: number } | null;
    solve: number[][];
    solve_final_cell: number[];
    walls: string[];
  }>;
}>("reference.json");

const cellEq = (a: Cell, b: number[]) => a[0] === b[0] && a[1] === b[1];

function checkStructural(
  got: ReturnType<typeof make>,
  want: {
    size: number;
    pair: string;
    entry: number[];
    exits: number[][];
    initial_heading: number;
    blocked: string[];
    dist: number[][];
    dist_entry: number;
    optimal_entry: { turn: number; step: number } | null;
    solve: number[][];
    solve_final_cell: number[];
    walls: string[];
  },
  at: string
): void {
  expect(got.w).toBe(want.size);
  expect(got.pair).toBe(want.pair);
  expect(cellEq(got.entry, want.entry), `${at}: entry`).toBeTrue();
  expect(got.exits.length).toBe(want.exits.length);
  for (let i = 0; i < want.exits.length; i++) {
    expect(cellEq(got.exits[i], want.exits[i]), `${at}: exit ${i}`).toBeTrue();
  }
  expect(initialHeading(got)).toBe(want.initial_heading);

  const blocked = [...got.blocked].sort();
  expect(blocked).toEqual(want.blocked);

  const dist = distanceField(got);
  expect(dist.size).toBe(want.dist.length);
  for (const [x, y, d] of want.dist) {
    expect(dist.get(`${x},${y}`)).toBe(d);
  }
  expect(dist.get(`${got.entry[0]},${got.entry[1]}`)).toBe(want.dist_entry);

  const opt = optimalAction(
    got,
    got.entry,
    initialHeading(got),
    distanceField(got)
  );
  if (want.optimal_entry === null) expect(opt).toBeNull();
  else {
    expect(opt).not.toBeNull();
    expect(opt![0]).toBe(want.optimal_entry.turn);
    expect(opt![1]).toBe(want.optimal_entry.step);
  }

  const { trace, final } = solve(got, got.entry, initialHeading(got));
  expect(trace.length).toBe(want.solve.length);
  for (let i = 0; i < want.solve.length; i++) {
    expect(trace[i][0]).toBe(want.solve[i][0]);
    expect(trace[i][1]).toBe(want.solve[i][1]);
  }
  expect(cellEq(final[0], want.solve_final_cell), `${at}: solve final cell`).toBeTrue();

  const walls = got
    .wallSegments()
    .map(
      ([x1, y1, x2, y2]) =>
        `${pyFloatStr(x1)},${pyFloatStr(y1)}>${pyFloatStr(x2)},${pyFloatStr(y2)}`
    )
    .sort();
  expect(walls).toEqual(want.walls);
}

describe("maze parity (all 200 manifest mazes)", () => {
  test("structure, oracle and solve traces match fixtures/golden", () => {
    expect(mazesFix.count).toBe(200);
    for (const want of mazesFix.mazes) {
      const spec: MazeSpec = {
        idx: want.idx,
        size: want.size,
        pair: want.pair,
        seed: BigInt(want.seed),
      };
      const got = makeMaze(spec);
      checkStructural(got, want, `maze#${want.idx}`);
    }
  });
});

describe("reference bases parity (40 seeds, 9x9)", () => {
  test("build_base sequence and rotations match fixtures/golden", () => {
    for (const want of refFix.bases) {
      const rng = new PyRandom(want.seed_index);
      const pair = rng.choice(Object.keys(EXIT_PAIRS));
      expect(pair).toBe(want.pair);
      const got = make(9, 9, pair, rng);
      checkStructural(got, want, `base#${want.seed_index}`);
    }
  });
});

describe("apply/rotate edge cases", () => {
  test("rotateWalls keeps cell indices and rotates walls", () => {
    const spec: MazeSpec = {
      idx: 0,
      size: 9,
      pair: "NW",
      seed: BigInt(mazesFix.mazes[0].seed),
    };
    const base = makeMaze(spec);
    for (const deg of [0, 90, 180, 270]) {
      const m = rotateWalls(base, deg);
      expect(m.w).toBe(base.w);
      expect(m.blocked.size).toBe(base.blocked.size);
      // rotation is a graph isomorphism: same number of open interior edges
      let openB = 0;
      let openR = 0;
      for (const k of base.blocked) {
        const [a, b] = k.split("|");
        const [ax, ay] = a.split(",").map(Number);
        const [bx, by] = b.split(",").map(Number);
        if (base.isOpen([ax, ay], [bx, by])) openB++;
        const rot = (c: Cell): Cell => {
          let [x, y] = c;
          for (let i = 0; i < (deg / 90) % 4; i++) [x, y] = [8 - y, x];
          return [x, y];
        };
        const [rax, ray] = rot([ax, ay]);
        const [rbx, rby] = rot([bx, by]);
        if (m.isOpen([rax, ray], [rbx, rby])) openR++;
      }
      expect(openR).toBe(openB);
    }
  });

  test("applyAction snaps off-lattice headings like Python round", () => {
    const spec: MazeSpec = {
      idx: 0,
      size: 9,
      pair: "NW",
      seed: BigInt(mazesFix.mazes[0].seed),
    };
    const m = makeMaze(spec);
    const h0 = initialHeading(m);
    // 225° off north: (h0+225)%360 snaps via half-even round
    const [, h] = applyAction(m, m.entry, h0, [225, 1]);
    expect(h % 90).toBe(0);
    expect([0, 90, 180, 270]).toContain(h);
  });
});
