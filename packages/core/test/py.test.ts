import { describe, expect, test } from "bun:test";
import {
  PyRandom,
  pyFixed1,
  pyRound,
  pyRoundInt,
  pyFloatStr,
  pyDumps,
  pyCanonicalDumps,
} from "../src/py";

describe("PyRandom (CPython random.Random parity)", () => {
  test("Random(0).random() stream", () => {
    const r = new PyRandom(0);
    expect(r.random()).toBe(0.8444218515250481);
    expect(r.random()).toBe(0.7579544029403025);
    expect(r.random()).toBe(0.420571580830845);
    expect(r.random()).toBe(0.25891675029296335);
    expect(r.random()).toBe(0.5112747213686085);
  });

  test("Random(0).choice(0..9) stream", () => {
    const r = new PyRandom(0);
    expect(r.choice([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])).toBe(6);
    expect(r.choice([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])).toBe(6);
    expect(r.choice([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])).toBe(0);
    expect(r.choice([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])).toBe(4);
    expect(r.choice([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])).toBe(8);
  });

  test("64-bit seed (two-word init_by_array key)", () => {
    const r = new PyRandom(17725994237439495539n);
    expect(r.random()).toBe(0.5490631532788395);
    expect(r.random()).toBe(0.6963786592310637);
    expect(r.random()).toBe(0.44484172416028744);
  });

  test("dataset draw sequence (seed 20260918)", () => {
    const rs = new PyRandom(20260918);
    expect(rs.choice([9, 11, 13])).toBe(11);
    expect(rs.choice([9, 11, 13])).toBe(9);
    expect(rs.choice([9, 11, 13])).toBe(13);
    expect(rs.choice([9, 11, 13])).toBe(9);
    expect(rs.choice([9, 11, 13])).toBe(13);

    const rp = new PyRandom(20260918);
    expect(rp.choice(["NW", "WS", "SE", "EN"])).toBe("SE");
    expect(rp.choice(["NW", "WS", "SE", "EN"])).toBe("WS");
    expect(rp.choice(["NW", "WS", "SE", "EN"])).toBe("NW");
    expect(rp.choice(["NW", "WS", "SE", "EN"])).toBe("EN");
    expect(rp.choice(["NW", "WS", "SE", "EN"])).toBe("SE");
  });

  test("_randbelow(13) with seed 7 is 5", () => {
    const r = new PyRandom(7);
    expect(r.randrange(13)).toBe(5);
  });
});

describe("number formatting (CPython str/round/format parity)", () => {
  test("f'{x:.1f}' — ties to even, -0.0 preserved", () => {
    const cases: Array<[number, string]> = [
      [0.25, "0.2"],
      [-0.25, "-0.2"],
      [0.75, "0.8"],
      [2.25, "2.2"],
      [1.15, "1.1"],
      [1.25, "1.2"],
      [1.35, "1.4"],
      [450.04, "450.0"],
      [-0.04, "-0.0"],
      [0.0, "0.0"],
      [-0.0, "-0.0"],
      [729.4123, "729.4"],
      [2.5, "2.5"],
      [3.5, "3.5"],
      [0.05, "0.1"],
      [0.15, "0.1"],
      [6.1, "6.1"],
      [123.4567, "123.5"],
      [-1.75, "-1.8"],
      [0.7, "0.7"],
    ];
    for (const [x, want] of cases) expect(pyFixed1(x)).toBe(want);
    // where JS toFixed disagrees with Python, prove the difference is real
    expect((0.25).toFixed(1)).toBe("0.3"); // JS rounds half away from zero
    expect(pyFixed1(0.25)).toBe("0.2"); // Python rounds half to even
  });

  test("round(x, n) — half-even on the exact value", () => {
    expect(pyRound(0.5196408529741863, 3)).toBe(0.52);
    expect(pyRound(0.4822916666666667, 3)).toBe(0.482);
    expect(pyRound(0.476, 3)).toBe(0.476);
    expect(pyRound(1234.5678, 2)).toBe(1234.57);
    expect(pyRound(2.5, 0)).toBe(2.0);
    expect(pyRound(3.5, 0)).toBe(4.0);
    expect(pyRound(0.000123456, 6)).toBe(0.000123);
    expect(pyRound(99.999999, 4)).toBe(100.0);
    expect(pyRound(12.345, 6)).toBe(12.345);
    expect(Math.round(2.5)).toBe(3); // JS rounds half up — NOT Python
    expect(pyRoundInt(2.5)).toBe(2); // the heading snap depends on this
    expect(pyRoundInt(3.5)).toBe(4);
    expect(pyRoundInt(2.5)).not.toBe(Math.round(2.5));
  });

  test("str(float) parity", () => {
    expect(pyFloatStr(1.0)).toBe("1.0");
    expect(pyFloatStr(0.8)).toBe("0.8");
    expect(pyFloatStr(0.005)).toBe("0.005");
    expect(pyFloatStr(900.0)).toBe("900.0");
    expect(pyFloatStr(-0.0)).toBe("-0.0");
    expect(pyFloatStr(1e16)).toBe("1e+16");
    expect(pyFloatStr(1e-5)).toBe("1e-05");
    expect(pyFloatStr(729.4123)).toBe("729.4123");
  });
});

describe("json parity", () => {
  test("dumps default separators", () => {
    expect(pyDumps({ turn: 0, rotation_deg: 90 })).toBe(
      '{"turn": 0, "rotation_deg": 90}'
    );
    expect(pyDumps({ a: [1, 2], b: "x" })).toBe('{"a": [1, 2], "b": "x"}');
    expect(pyDumps({ turn: -90, step: 1 })).toBe('{"turn": -90, "step": 1}');
    expect(pyDumps([])).toBe("[]");
    expect(pyDumps({})).toBe("{}");
  });

  test("canonical form (sorted, tight, ascii-escaped)", () => {
    expect(pyCanonicalDumps({ b: 1, a: [2, { z: 1, y: 2 }] })).toBe(
      '{"a":[2,{"y":2,"z":1}],"b":1}'
    );
    expect(pyCanonicalDumps("café ☃")).toBe('"caf\\u00e9 \\u2603"');
    expect(pyCanonicalDumps('quote " back\\slash')).toBe(
      '"quote \\" back\\\\slash"'
    );
  });
});
