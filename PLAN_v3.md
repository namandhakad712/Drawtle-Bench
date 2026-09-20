# Drawtle Bench — v3 Plan: Operable, Standardised, Test-Mode Bench

Status: v2 delivered and committed (`e181e10`). The measurement works and the
control centre no longer hangs. v3 is about **operability**: every control the
bench needs is in the panel, mock and live data never contaminate each other,
docs match reality, and a single command answers "is it ready".

This plan is a contract. Milestones are independently shippable; each has
acceptance criteria that a test asserts.

## Decisions locked (20 Sept 2026)

| Decision | Choice |
|---|---|
| Theme | **Build dark mode behind a toggle, and update `DESIGN.md`** to record the reversal honestly rather than leaving the doc claiming "no dark surfaces". |
| Docker engine | **The control centre may start the engine, off by default.** Gated behind a setting, because it lets anything that can reach the port launch processes. |
| Test vs live | **One results dir with a `mode` field and a header filter** — not two directories. One definition of "runs", one analytics pipeline. |
| Scope/sequencing | **Pending** — plan shown for review before any code is written. |

---

---

## 0. What I verified in the repo before writing this

So the plan is grounded, not aspirational:

| Thing | State today |
|---|---|
| Tabs | 9: Overview, Providers, Models, Launch, Results, Replays, Storyboard, Logs, System. **No Settings, no Test Mode.** |
| Provider/model discovery + tick-to-adopt | **Built** (`/api/probe`, `/api/adopt` with `ids`) |
| Provider "test connection" | **Built** (`/api/probe?test_only=1`) |
| Leaderboard bar chart | **Built** (horizontal bars, CI whiskers, graded colours) |
| Live logs | **Built** (Logs tab, polling + disk `logs/<run_id>.log`) |
| Delete a run / bulk "delete incomplete" | **Built** (`/api/run/delete`, Results toolbar) |
| API-key entry in the UI | **Missing** — System only prints the credentials *path* |
| Docker control from the UI | **Missing** — status is reported, nothing can be started |
| Test vs live separation | **Missing** — mock and real runs share one results dir and one leaderboard |
| Theme | **Light only, deliberately.** `DESIGN.md` argues "no dark surfaces" because numbers on dark panels are harder to read |
| Storyboard | Run **cards only** — no images at all |
| Frame lightbox | **Missing** — Replays shows thumbnails, nothing enlarges |
| Settings persistence | **Missing** |
| Data-integrity surface | **Partial** — `bench.py status` and `scan_jsonl` exist; no dashboard view |
| Docs | 10 root `.md` files + `docs/build.py` (zero-dep) → `docs/*.html` for Pages. **Docs are generated: edit `.md`, then run the build.** |
| `results/` hygiene | 42 files; suite leftovers (`cc-test-*`, `log-test`, `run-mock-*`) sit beside the committed demo artifacts |
| `/api/runs` cost | `enumerate_runs` re-scans every JSONL on **every poll** — no mtime cache |
| Docker daemon | **Not running right now**; compose validates; `sandbox: none` reported honestly |

**Consequence:** most of the "features" list is already built. The real work is
settings/test-mode/isolation, the missing surfaces (keys, docker control,
lightbox, integrity, analytics), and optimisation. I will not rebuild what exists.

---

## 1. Milestones

### M1 — Test mode and run provenance (the load-bearing change)
Every run records `mode: live | test`, decided at launch (mock backend ⇒ test).
A global **Test Mode** toggle in the header:
- **off (default):** every view shows live runs only; test runs are invisible.
- **on:** shows test runs only, with a persistent amber banner, and the
  leaderboard is clearly labelled as not a result.

Acceptance: launching a mock run does not change the live leaderboard; toggling
shows it; the toggle state is visible on every tab.

*Design note:* a `mode` field + filter, **not** two directories. Separate dirs
would fragment analytics and make "runs" mean two different things.

### M2 — Settings tab + user settings
A new tab: theme, test-mode default, results retention, probe timeout, default
launch backend/model/dataset, log follow. Persisted to the **overlay config dir**
(outside the repo) — the same rule the provider overlay already follows, so
settings never dirty the working tree.

### M3 — Docker & sandbox control
- Status: daemon up/down, image present, egress-proxy container state, allow-list.
- **Start engine** button → launches Docker Desktop and polls until the daemon
  answers, with a spinner and a clear failure message.
- Explain, in the UI, what `sandbox: none` means for a committed result.
- `docker/README` surfaced in the panel.

### M4 — API keys in the UI
Add/update/test/remove keys per provider, writing to the credentials store.
**Keys are never rendered back** — only `present: yes/no` and the source
(`env` / `file`). A key field that echoes is a key that leaks into screenshots.

### M5 — Storyboard + lightbox
- Storyboard gains real frames (first frame of each episode) instead of text-only
  cards.
- One shared **lightbox**: click any frame anywhere (Storyboard, Replays) to view
  it large, with keyboard nav (←/→/Esc) and the turn's parsed action beside it.

### M6 — Analytics, monitoring, integrity
- **Analytics** view: per-model/per-maze/per-size breakdowns, error taxonomy,
  MDI, cost, and the lag-sensitivity curve — as charts, not just tables.
- **Integrity panel**: bad JSONL lines, orphaned runs, summary↔status mismatches,
  dataset-hash mismatches, missing frames for a vision run. "Unknown is not zero"
  stays the rule: a gap is shown as a gap.
- **Monitoring**: live job count, disk use, frame-cache size, last-N failures.

### M7 — Results hygiene, filters, per-session saving
- One canonical mock demo kept; **all other mock/test artifacts removed** from
  `results/` (and the suite made to clean up after itself — it currently leaves
  `cc-test-*` behind, which is how this mess happened).
- Filters on Results: status, model, provider, mode, date; sortable columns.
- Per-session export (CSV/JSON/HTML) of the filtered view.
- Retention policy: delete runs older than N days, and "delete all test runs".

### M8 — Performance, docs, one-command readiness
- **Concrete targets, not vibes:** `/api/runs` cached by directory mtime;
  `/api/leaderboard` likewise; incremental log tailing (send only new lines);
  frames served from a static route instead of re-embedded per response.
  Target: every Overview call under 50 ms warm, and no full-results rescan per poll.
- Update `README`, `GUIDE`, `GETTING_STARTED`, `TESTING`, `DESIGN`, `CHANGELOG`,
  `PLAN_v3`; then run `python docs/build.py` so `docs/*.html` matches.
- **New: `bench.py doctor`** — one command that runs every preflight check
  (python, rasteriser, dataset, docker, keys, disk, integrity) and prints a
  single READY / NOT-READY verdict with the blocking reason. This is the
  question you keep asking, as a command instead of a conversation.

---

## 2. My additions (not on your list, and why they matter)

1. **`bench.py doctor`** — the "is it ready?" answer, scriptable, in CI.
2. **Run provenance in every artifact.** A result file should say whether it is
   live or test *in itself*, so an exported file can never be mistaken for a real
   result once it has left this machine. This is the same principle as the
   existing "unknown is not zero" rule.
3. **The test suite must clean up after itself.** Right now it leaves runs in
   `results/`; that is the root cause of the junk you asked me to delete. Fixing
   the cause beats deleting the symptom.
4. **A single `frames` static route** instead of embedding base64 PNGs in JSON.
   This is the biggest real speed win available and it shrinks every replay
   response.
5. **Key redaction as a test.** Assert that no endpoint can return a key value.
6. **`--mode` in `bench.py run`** so test runs are correctly stamped at the source
   rather than inferred afterwards.

---

## 3. Where I would push back

- **Dark mode contradicts a documented decision.** `DESIGN.md` argues against
  dark surfaces *because this is a numbers tool*. I will build the toggle you
  asked for, but I will also record the reversal in `DESIGN.md` rather than
  leave the doc lying about the design. Flagging it rather than silently
  reversing it.
- **"Start Docker engine" means launching a GUI desktop app from the server.**
  I can do it on Windows, but it is a real privilege escalation for a local web
  server: anything that can reach the port could launch processes. I will gate it
  behind a setting that is **off by default**.
- **This is not one sitting.** Eight milestones with tests, docs and a
  re-verified suite is several sessions of work. Anyone who tells you otherwise
  is guessing. The ordering above is chosen so each milestone is usable on its own.

---

## 4. Build order

`M1 → M2 → M7 → M3 → M4 → M5 → M6 → M8`

M1 first because everything else depends on knowing which runs are real. M7 early
because deleting the junk before adding more surfaces keeps the mess from growing.
M8 last because the docs and the doctor command should describe the finished thing.
