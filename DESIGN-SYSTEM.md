# DESIGN-SYSTEM.md — the Drawtle Bench site design system

(The historical benchmark-design document lives in `DESIGN.md`; this
file is about the published site's visual system.)

The published site (`packages/cli/src/site.ts`) is a build of recorded
measurements; its design system exists to make those measurements
legible. Every choice below serves one of two goals: **trust** (you can
see where a number came from) and **understanding** (a first-time
visitor can feel what the bench measures in under a minute). The system
is deliberately small — four typefaces, five semantic colours, three
motion rules — because a benchmark's site should read like an
instrument, not a product landing page.

## Typography

| Role | Face | Why |
|---|---|---|
| Display (h1–h3, nav brand, KPI labels, buttons) | **Space Grotesk** 500/700 | Geometric with technical warmth; signals "instrument" without the coldness of a pure grotesque. |
| UI / body (leaderboard, task page) | **Inter** 400/500/650 | Maximum legibility at small sizes; neutral enough that data, not the type, is the voice. |
| Reading surface (paper.html) | **Source Serif 4** 400/600 | The paper is prose; serif at 16px signals "read this slowly" and separates it from the dashboard pages. |
| Numbers, code, verdict values | **JetBrains Mono** 400/650 | Tabular by nature — CIs, token counts and coordinates align in columns; used for every number that could be compared. |

Numeric text uses `font-variant-numeric: tabular-nums` everywhere a
digit appears beside another digit.

## Colour — semantics before aesthetics

The palette is a single dark surface (`#0f141a` background, `#151c24`
cards, `#222b34` lines) so that the only saturated colours on any page
are **meanings**. A verdict colour means the same thing in a frame, on
a minimap, in a chart bar, in a pill and in the demo:

| Token | Value | Meaning (the psychology) |
|---|---|---|
| `--ok` | `#58c98f` green | Success/progress — the only "reward" colour on the site; used exclusively for distance-decreased outcomes. |
| `--wall` | `#ff6b5e` red-orange | Danger/stop — walking into a wall. Red-orange rather than pure red so it reads as "blocked", not "system error". |
| `--stale` | `#e5b95e` amber | Caution without harm — a legal move that didn't help. Amber says "not wrong, not useful". |
| `--info` | `#7ec8ff` blue | Informational — the scorer's view, `no_exit_reachable` (nobody's fault; the world rotated). Also the link/accent colour: the scorer is the reader's ally. |
| `--invalid` | `#e070b8` magenta | Format error — deliberately the *least* natural colour in the set, because it is the verdict a well-behaved model should never produce. |

Green exits and the red START square in the rendered mazes predate the
site and are pinned by fixture; the site's `--ok`/`--wall` tokens were
chosen to sit beside them without collision.

## Motion

Three rules, all disabled under `prefers-reduced-motion`:

1. **Reveal on scroll** (fade + 14px rise, 600ms) — paces the page so
   each figure arrives as a unit; nothing animates until visible.
2. **Count-up KPIs** (950ms, cubic ease-out) — numbers that grow feel
   measured, not asserted; the final value is in the HTML, so
   no-JS/print users see it immediately.
3. **Bar growth on reveal** (750ms, scaleX from left) and the dashed
   flow arrows (1.1s loop) — direction-of-growth animations that
   restate the data's direction, never decoration.

The episode player's autoplay-on-scroll fires once at 45% visibility.
The demo never animates game state: verdicts appear instantly, because
the bench's whole claim is that scoring is mechanical.

## The two interactive instruments

- **The player** (task page) — replays a measured episode turn by turn
  from `run.jsonl`: model view (heading never drawn) beside scorer view
  (heading arrow, BFS distance, ghost target). It exists to make the
  frame/reply/verdict triple inspectable.
- **The demo** ("You try it") — the visitor plays four real rounds of a
  measured episode at its recorded rotations. Every reachable frame
  variant is precomputed at build time (frames depend only on rotation
  × the four headings); the client recomputes verdicts from embedded
  BFS tables and overlays the visitor's own move as a ghost or ✕. The
  final screen compares the visitor with the measured model and the
  optimal policy on the same task. The demo is the site's thesis in
  miniature: you cannot beat a rotating world from memory either.

## Rules of the build

- No runtime dependencies, no client analytics, no external JS. Fonts
  are the only third-party request.
- Every number, frame and reply is interpolated from the committed
  results at build time; the site cannot disagree with the repo.
- No-JS fallbacks: the static turn cards, the tables and every
  readable figure carry the full story; the player and demo enhance,
  they do not gate.
- Print: controls hidden, figures kept whole, no break after headings;
  the paper prints as a plain serif document.
