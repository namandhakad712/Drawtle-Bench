/**
 * Python compatibility layer: the exact arithmetic v2 relied on.
 *
 * v3 is a port, and the parity proof (fixtures/golden) is byte-level. That
 * requires reproducing CPython behaviours ECMAScript does not guarantee:
 *
 *  - `random.Random` — MT19937 seeded exactly as CPython seeds it
 *    (`init_by_array` over the 32-bit words of the absolute value,
 *    little-endian), with `random()`, `getrandbits(k<=32)`, the
 *    `_randbelow` rejection loop, one-argument `randrange`, and `choice`.
 *    Every draw v2 ever made runs through this.
 *  - `f"{x:.1f}"` — the correctly-rounded ONE-decimal representation of the
 *    exact binary value, ties to even. `toFixed` differs on exact midpoints
 *    (0.25 → "0.2" in Python, "0.3" in JS) and those DO occur: projected
 *    coordinates land on lattice quarters. `pyFixed1` does the real thing
 *    with BigInt, as does `pyRound` (Python `round(x, n)`, half-even on the
 *    exact value — `Math.round(2.5)` is 3, Python's is 2, and the heading
 *    snap depends on it).
 *  - `str(float)` / `repr(float)` — shortest round-trip digits with Python's
 *    integral ('1.0') and exponent forms (`pyFloatStr`).
 *  - `json.dumps` — default separators for prompt text (", ", ": "),
 *    insertion key order; and the canonical form used by the transcript
 *    pool (sorted keys, tight separators, ensure_ascii escaping).
 *
 * Verified against fixtures/golden by packages/core/test/py.test.ts.
 */

// ---------------------------------------------------------------- RNG ----

const N = 624;
const MATRIX_A = 0x9908b0df;
const UPPER_MASK = 0x80000000;
const LOWER_MASK = 0x7fffffff;

export class PyRandom {
  private mt = new Uint32Array(N);
  private mti = N + 1;

  constructor(seed: number | bigint) {
    this.seed(seed);
  }

  /** CPython `random.seed(int)`: abs, split into 32-bit words LE, then
   * MT19937 `init_by_array`. seed 0 → key [0]. */
  seed(a: number | bigint): void {
    let n: bigint;
    if (typeof a === "number") n = BigInt(Math.trunc(a));
    else n = a;
    if (n < 0n) n = -n;
    const key: bigint[] = [];
    let x = n;
    while (x > 0n) {
      key.push(x & 0xffffffffn);
      x >>= 32n;
    }
    if (key.length === 0) key.push(0n);
    this.initByArray(key);
  }

  private initGenrand(s: number): void {
    this.mt[0] = s >>> 0;
    for (let i = 1; i < N; i++) {
      this.mt[i] =
        (Math.imul(1812433253, this.mt[i - 1] ^ (this.mt[i - 1] >>> 30)) +
          i) >>> 0;
    }
    this.mti = N;
  }

  /** MT19937 init_by_array (Matsumoto & Nishimura), CPython's seeding path. */
  private initByArray(key: bigint[]): void {
    this.initGenrand(19650218);
    let i = 1;
    let j = 0;
    let k = N > key.length ? N : key.length;
    const m = this.mt;
    for (; k; k--) {
      const prev = m[i - 1];
      let v = (m[i] ^ (Math.imul(1664525, prev ^ (prev >>> 30)) >>> 0)) >>> 0;
      v = (v + Number(key[j] & 0xffffffffn) + j) >>> 0;
      m[i] = v;
      i++;
      j++;
      if (i >= N) {
        m[0] = m[N - 1];
        i = 1;
      }
      if (j >= key.length) j = 0;
    }
    for (k = N - 1; k; k--) {
      const prev = m[i - 1];
      let v = (m[i] ^ (Math.imul(1566083941, prev ^ (prev >>> 30)) >>> 0)) >>> 0;
      v = (v - i) >>> 0;
      m[i] = v;
      i++;
      if (i >= N) {
        m[0] = m[N - 1];
        i = 1;
      }
    }
    m[0] = 0x80000000;
  }

  private next32(): number {
    const m = this.mt;
    let y: number;
    if (this.mti >= N) {
      if (this.mti === N + 1) this.initGenrand(5489);
      for (let kk = 0; kk < N - 397; kk++) {
        y = (m[kk] & UPPER_MASK) | (m[kk + 1] & LOWER_MASK);
        m[kk] = m[kk + 397] ^ (y >>> 1) ^ (y & 1 ? MATRIX_A : 0);
      }
      for (let kk = N - 397; kk < N - 1; kk++) {
        y = (m[kk] & UPPER_MASK) | (m[kk + 1] & LOWER_MASK);
        m[kk] = m[kk + (397 - N)] ^ (y >>> 1) ^ (y & 1 ? MATRIX_A : 0);
      }
      y = (m[N - 1] & UPPER_MASK) | (m[0] & LOWER_MASK);
      m[N - 1] = m[396] ^ (y >>> 1) ^ (y & 1 ? MATRIX_A : 0);
      this.mti = 0;
    }
    y = m[this.mti++];
    y ^= y >>> 11;
    y ^= (y << 7) & 0x9d2c5680;
    y ^= (y << 15) & 0xefc60000;
    y ^= y >>> 18;
    return y >>> 0;
  }

  /** CPython `random.random()` — two 32-bit draws, 53-bit result. */
  random(): number {
    const a = this.next32() >>> 5;
    const b = this.next32() >>> 6;
    return (a * 67108864.0 + b) * (1.0 / 9007199254740992.0);
  }

  /** CPython `random.getrandbits(k)` for 1 <= k <= 32. */
  getrandbits(k: number): number {
    if (k < 1 || k > 32) throw new RangeError("getrandbits: 1..32 only");
    return this.next32() >>> (32 - k);
  }

  /** CPython `_randbelow(n)` via the getrandbits rejection loop.
   *  No shortcuts: n=1 still draws (rejecting 1-bits) exactly as
   *  _randbelow_with_getrandbits does — the word consumption is part of the
   *  stream every later draw sees. */
  private randbelow(n: number): number {
    if (n <= 0) throw new RangeError("randbelow needs a positive bound");
    let bl = 0; // n.bit_length()
    let v = n;
    while (v > 0) {
      bl++;
      v >>>= 1;
    }
    let r = this.getrandbits(bl);
    while (r >= n) r = this.getrandbits(bl);
    return r;
  }

  /** CPython `random.randrange(n)` (single argument). */
  randrange(n: number): number {
    return this.randbelow(n);
  }

  /** CPython `random.choice(seq)` — `seq[_randbelow(len(seq))]`. */
  choice<T>(seq: readonly T[]): T {
    return seq[this.randbelow(seq.length)];
  }
}

// ------------------------------------------------------- number text ----

/** Exact binary decomposition: |x| = m * 2^e, m integral (BigInt). */
function decompose(x: number): { m: bigint; e: number } {
  const buf = new ArrayBuffer(8);
  new Float64Array(buf)[0] = x;
  const bits = new BigUint64Array(buf)[0];
  const exp = Number((bits >> 52n) & 0x7ffn);
  const frac = bits & 0xfffffffffffffn;
  if (exp === 0) return { m: frac, e: -1074 };
  return { m: frac + (1n << 52n), e: exp - 1075 };
}

/** round(x * 10^digits) half-even, exact; digits >= 0. Signed result. */
function roundScaled(x: number, digits: number): bigint {
  const neg = Object.is(x, -0) || x < 0;
  const ax = Math.abs(x);
  const { m, e } = decompose(ax);
  const mult = 10n ** BigInt(digits);
  if (e >= 0) {
    const q = (m << BigInt(e)) * mult;
    return neg ? -q : q;
  }
  const num = m * mult; // |x| * 10^d = num / 2^(-e)
  const div = 1n << BigInt(-e);
  let q = num / div;
  const r = num % div;
  const twice = r * 2n;
  if (twice > div || (twice === div && (q & 1n) === 1n)) q += 1n;
  return neg ? -q : q;
}

/** Python `round(x, digits)` for digits >= 0 (half-even, exact value). */
export function pyRound(x: number, digits: number): number {
  if (!Number.isFinite(x)) return x;
  const q = roundScaled(x, digits);
  return Number(q) / 10 ** digits;
}

/** Python `round(x)` (no digits) — integer, half-even. */
export function pyRoundInt(x: number): number {
  if (!Number.isFinite(x)) return x;
  return Number(roundScaled(x, 0));
}

/** Python `f"{x:.1f}"` — one decimal, ties to even, "-0.0" preserved. */
export function pyFixed1(x: number): string {
  if (Number.isNaN(x)) return "nan";
  if (!Number.isFinite(x)) return x > 0 ? "inf" : "-inf";
  const q = roundScaled(x, 1);
  const neg = q < 0n || (q === 0n && (x < 0 || Object.is(x, -0)));
  const a = q < 0n ? -q : q;
  const whole = a / 10n;
  const frac = a % 10n;
  return `${neg ? "-" : ""}${whole}.${frac}`;
}

/**
 * Python `str(float)`: shortest round-trip digits; integral values get
 * '.0'; Python's exponent thresholds (|x| >= 1e16 or < 1e-4 → e-notation
 * with at least two exponent digits).
 */
export function pyFloatStr(x: number): string {
  if (Number.isNaN(x)) return "nan";
  if (x === 0) return Object.is(x, -0) ? "-0.0" : "0.0";
  if (!Number.isFinite(x)) return x > 0 ? "inf" : "-inf";
  const ax = Math.abs(x);
  if (Number.isInteger(x) && ax < 1e16) {
    return `${x < 0 ? "-" : ""}${ax.toFixed(1)}`;
  }
  if (ax >= 1e-4 && ax < 1e16) {
    return x.toString();
  }
  const s = x.toExponential();
  const [mant, expRaw] = s.split("e");
  const sign = expRaw[0] === "-" ? "-" : "+";
  const digits = expRaw.slice(1).replace(/^0+/, "") || "0";
  return `${mant}e${sign}${digits.padStart(2, "0")}`;
}

// ---------------------------------------------------------------- JSON --

export type Json =
  | null
  | boolean
  | number
  | string
  | Json[]
  | { [k: string]: Json };

function asciiEscape(s: string): string {
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

function encNum(n: number): string {
  if (Number.isInteger(n) && Math.abs(n) < 1e16) return n.toString();
  return pyFloatStr(n);
}

/**
 * Python `json.dumps(x)` with DEFAULT settings (no sort_keys, separators
 * (", ", ": "), ensure_ascii). Key order is insertion order — build objects
 * in the same order Python built its dicts.
 */
export function pyDumps(x: Json): string {
  if (x === null) return "null";
  if (typeof x === "boolean") return x ? "true" : "false";
  if (typeof x === "number") return encNum(x);
  if (typeof x === "string") return asciiEscape(x);
  if (Array.isArray(x)) {
    if (x.length === 0) return "[]";
    return "[" + x.map((v) => pyDumps(v)).join(", ") + "]";
  }
  const keys = Object.keys(x);
  if (keys.length === 0) return "{}";
  return (
    "{" +
    keys.map((k) => `${asciiEscape(k)}: ${pyDumps(x[k] as Json)}`).join(", ") +
    "}"
  );
}

/**
 * The canonical form the transcript pool hashes:
 * `json.dumps(content, sort_keys=True, separators=(",", ":"))`.
 */
export function pyCanonicalDumps(x: Json): string {
  if (x === null) return "null";
  if (typeof x === "boolean") return x ? "true" : "false";
  if (typeof x === "number") return encNum(x);
  if (typeof x === "string") return asciiEscape(x);
  if (Array.isArray(x)) {
    if (x.length === 0) return "[]";
    return "[" + x.map((v) => pyCanonicalDumps(v)).join(",") + "]";
  }
  const keys = Object.keys(x).sort();
  if (keys.length === 0) return "{}";
  return (
    "{" +
    keys.map((k) => `${asciiEscape(k)}:${pyCanonicalDumps(x[k] as Json)}`).join(",") +
    "}"
  );
}
