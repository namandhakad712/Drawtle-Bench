# DEBUG.MD — Consolidated Debug Report (verified + fixed)

**Date:** 2026-09-21 (second pass)
**Base:** `master` @ `742cbc9` — v2.9.0
**Scope:** Runtime, model transport, data/metrics, web, Docker boundaries
**Method:** Every citation re-checked against the current checkout line by line;
every open defect in the previous pass was either confirmed-and-fixed or
excluded with the reason. This document now records the *current* state, not
the audit state. Anything below marked ✅ is fixed and verified by a green test
run.

---

## Executive summary

The bench is in good shape after v2.8.4–v2.9.0. The first pass of this report
was honest in its substance but stale in four conclusions: its "current
blocker" (no rasteriser) was true for the default interpreter yet masked by a
cache hit; its M1 (agnes-3.0-flash missing `image_in`) was contradicted by the
very config it cited; its verification-log row on `BadStatusLine`/`IncompleteRead`
was wrong (both are `HTTPException` subclasses and were already caught); and its
line numbers did not match HEAD.

**What the second pass found and fixed (all verified green):**

| # | Defect | Status |
|---|---|---|
| H1 | `max_tokens_per_episode` read but never enforced | ✅ fixed + `token_cap_hit` recorded |
| H2 | Crashed run invisible to `bench.py report` (no summary) | ✅ fixed: partial summary aggregated on error |
| M2 | UID 1000 hardcoded in every Docker artefact | ✅ fixed (`ARG UID`, compose `${UID:-1000}`) |
| M3 | `save_summary` non-atomic | ✅ fixed (`runstate.atomic_write_json`) |
| M4 | `fetch_models` no hard deadline | ✅ fixed (thread deadline) |
| M5 | `run_dataset` left `status=started` on non-KI exceptions | ✅ fixed (marks `error`) |
| M6 | Docker env passthrough covered 4 of ~20 providers | ✅ fixed (all provider keys) |
| L1 | `supervisor.reap()` dead code | ✅ fixed (called from `list()`) |
| L2 | `bench.py` imported the web stack unconditionally | ✅ fixed (lazy import in `cmd_serve`) |
| L3 | BOOT-state XSS (`json.dumps` into `<script>`) | ✅ fixed (`\u003c`/`\u003e`/`\u0026`) |
| L4 | `_TRACKED_CACHE` never invalidated | ✅ fixed (refresh per `/api/runs`) |
| L5 | `store_key` no fsync before `os.replace` | ✅ fixed (+ `clear_key` symmetry) |
| L6 | Unpinned optional deps | ✅ fixed (bounded ranges) |
| — | **NEW** `stats.aggregate` reported `status=unknown` for every nested (production) summary | ✅ fixed — read the required status in the right place |

One remaining environment fact (not a code defect): **the default `python`
`C:\Python314` has no rasteriser** (no cairosvg, no playwright); the test venv
(`.workbuddy-ai\...\envs\default`) has playwright + a cached Chromium and is
usable. `bench.py run` with a real backend now refuses *before* the warmup call
if the invoking interpreter cannot rasterise — the operator must run via the
venv or `pip install cairosvg` into `C:\Python314`.

---

## The incidents

### Incident A — rasteriser (refined; the old conclusion was half right)

First pass: "no rasteriser; preflight FAILs (reproduced today)". Re-verified:

- The exact FAIL text is real (`drawtle/frames.py:133-138` raises it), and with
  the interpreter that has no rasteriser the gate genuinely fails.
- **But the first pass reproduced it while a rasteriser WAS installed**: the
  80 KB PNG check passed against a hash-keyed frame cache written by a
  *different* interpreter (the venv). `results/frames/_preflight/` is a shared
  cache across interpreters, so a cache hit made `preflight` say `[ok]` for an
  interpreter that cannot rasterise at all. **That masking is now fixed** —
  `analysis/preflight.py` renders into a per-process unique probe name, so it
  proves the interpreter that is running the check, not the one that ran a
  previous one.
- Current machine state: `C:\Python314` → `[FAIL] no rasteriser` (correct),
  venv → `[ok] 80,430 byte PNG` (correct).
- `bench.py run` now hard-refuses a real backend when its own interpreter has no
  usable rasteriser, before any warmup call is spent.

### Incident B — the paid run that died at turn 58 (historical, both fixes verified)

`run-agnes-3.0-flash-1789922909` died at turn 58 of episode 6 with
`RemoteDisconnected`. The run directory is absent from this checkout, so the
per-run numbers stay historical. The two root causes are verified at HEAD:

1. `docker/egress_proxy.py:112-133` — `_relay` now uses `EGRESS_IDLE_TIMEOUT_S`
   (default 600 s) instead of a hardcoded 30 s ✅
2. `drawtle/models.py:362-363` — retry tuple now includes `ConnectionError` and
   `http.client.HTTPException`; `RemoteDisconnected` is both (probed: not a
   `URLError`) ✅. Note the first-pass verification-log row claimed
   `BadStatusLine`/`IncompleteRead` were "still uncaught" — that is wrong; both
   are `HTTPException` subclasses and ARE caught. Only `JSONDecodeError` was
   outside the tuple.

### Incident C — the v2.9.0 operator session (verified)

`CHANGELOG.md:3-9` matches: agnes stalled past 60 s, nararouter 500, intern
`invalid on every turn`; v2.9.0's READY gate / thinking split / deadlines are
the fix. Also documented: the file-revert "ghost" that ate three real runs,
now visible via the ghost guard ✅.

---

## Previously "open" — now fixed and re-verified

### H1 · `max_tokens_per_episode` was read but never enforced
**Fix:** `drawtle/runner.py` — the turn loop now breaks when
`ep_tokens >= max_tokens` (checked before the run-wide budget line), and the
episode summary records `token_cap_hit: true` so a capped episode is
distinguishable from an unfinished one.

### H2 · A crashed run was invisible to `bench.py report`
**Fix:** `bench.py` error branch now aggregates the turns that landed
(`ME.aggregate` over the JSONL + episodes from `done.json`) and writes
`summary.json` via `save_summary`; status stays `error`, so `leaderboard` still
excludes it and `report` can read it. Verified end-to-end (crash mid-run →
status `error`, partial summary, `run_summary_json` 200, excluded=1).
**Also fixed:** `drawtle/stats.py` — the summary's `status` field was `unknown`
for every **nested** (production) run because `read_status` was resolved
against the run directory instead of the results base. A successful mock run
now writes `status: "success"` in its summary (was `unknown`).

### M1 · ~agnes-3.0-flash missing `image_in`~ — EXCLUDED, claim was wrong
The first pass said "backfill `image_in`". The cited source of truth
(`~/.dsh/settings.yaml` agnes block) **disagrees**: every `agnes-3.0-flash`
entry in it declares `input: []`, while `agnes-2.5-flash` and `agnes-3-flash`
declare `input: [text, image]`. The registry entry `["thinking","tool_use"]`
is faithful to the config; backfilling would claim a capability the harness
config says the model does not have. Not a defect.

### M2 · UID 1000 hardcoded
**Fix:** `docker/Dockerfile` + `docker/egress-proxy.Dockerfile` take
`ARG UID` (default 1000); `docker/docker-compose.yml` services use
`user: "${UID:-1000}:${UID:-1000}"` and pass `UID` through `build.args`.

### M3 · `save_summary` non-atomic
**Fix:** `drawtle/measures.py` delegates to `runstate.atomic_write_json`
(unique temp, flush+fsync, retried replace).

### M4 · `fetch_models` no hard deadline
**Fix:** `drawtle/catalog.py` — `_request_with_deadline` runs `_request` in a
daemon thread and joins on the timeout; a stalling discovery endpoint now
returns `{ok: False, error: "TimeoutError ... stalled"}` instead of hanging.

### M5 · `run_dataset` caught only `KeyboardInterrupt`
**Fix:** `drawtle/runner.py` — a new `except Exception` branch flushes the
checkpoint/transcript and `mark_finished(STATUS_ERROR, ...)` (only when status
is still `started`), then re-raises. A non-CLI caller can no longer leave a
dead run looking unfinished. The CLI's own branch still re-marks (idempotent).

### M6 · Docker env passthrough covered 4 of ~20 providers
**Fix:** `docker/docker-compose.yml` forwards every `key_env` name declared in
`drawtle/providers.json` (OPENAI, ANTHROPIC, GEMINI/GOOGLE, INTERNLM/INTERN,
VLLM, OPENROUTER, GROQ, DEEPSEEK, MISTRAL, TOGETHER, AGNES, NARAROUTER,
POOLSIDE, INFERX, OPENCODE, OPENCODE_CUSTOM, ATRIA_DAWN_INTERLM,
TOKEN_HARBOR, INSTITUTE_OF_FOUNDATION_MODELS).

### L1 · `supervisor.reap()` dead code
**Fix:** `web/supervisor.py` — `list()` (the `/api/jobs` heartbeat) calls
`reap()` first; finished jobs older than 6 h are dropped from memory.

### L2 · `bench.py` imported the web stack unconditionally
**Fix:** `bench.py` — `from web import server as SRV` moved into `cmd_serve`.
`analysis/check_docker.py` still sees the import (it scans all lines), so the
Docker consistency gate still requires `web/` in the image.

### L3 · BOOT-state XSS
**Fix:** `web/views.py` — the bootstrap JSON is re-escaped (`&`→`\u0026`,
`<`→`\u003c`, `>`→`\u003e`) in plain Python before the f-string injects it, so
it can never break out of the `<script>` block. `check_dashboard_js` and the
44-checks browser suite pass.

### L4 · `_TRACKED_CACHE` never invalidated
**Fix:** `web/server.py` — `tracked_runs(dir_, refresh=True)` on `/api/runs`
resets the module cache before recomputing, so a run committed mid-session
stops looking untracked on the next poll.

### L5 · `store_key` lacked fsync
**Fix:** `drawtle/catalog.py` — `store_key` **and** `clear_key` flush+fsync
before `os.replace`.

### L6 · Unpinned optional deps
**Fix:** `pyproject.toml` — `cairosvg>=2.7,<3`, `playwright>=1.40,<2` (bounded
so a raster upgrade cannot silently change what a replayed frame looks like).

### Incident B · subprocess output decoded with the console codepage (crash after `docker compose build`)
The control-console console printed a raw
`Exception in thread Thread-33 (_readerthread): UnicodeDecodeError: 'charmap'
codec can't decode byte 0x81 in position 20588` right after the health check.
Cause: `subprocess.run(..., text=True)` **without** `encoding` decodes child
output with the console codepage (cp1252 here). A `docker compose build` log
>20 KB containing any byte that cp1252 leaves undefined kills the internal
reader thread — which makes `subprocess.run` return **empty** output with rc 0
(the build itself succeeds, the Docker panel shows nothing) and dumps the
traceback onto the server console. Same latent bug in every other text-mode
call (git, compose ps/images, docker info) — only the build/smoke logs are big
enough to hit it in practice.
**Fix:** every text-mode subprocess call now passes
`encoding="utf-8", errors="replace"` — `web/server.py` (git ls-files, compose
images/ps, compose actions) and `drawtle/sandbox.py` (docker info) — matching
the pattern `web/supervisor.py` already used. Reproduced byte-for-byte (0x81 at
position 20588) with the old pattern: `out_len=0` while rc=0; with the new
pattern the full decoded stream comes back (`out_len=20689`, tail intact).

---

## What is already solid (re-verified, unchanged)

- **Leaderboard integrity** — an error run with a partial summary is excluded
  (`excluded_runs`), and the new partial-summary path does not change that.
- **Provenance is probed, not assumed** — `SBX.provenance()` + `bench.py` prints
  the isolation level before a paid call.
- **Resume safety** — `prev_hash != dataset_hash` refuses to splice datasets.
- **Safe defaults** — `prune_runs` `dry_run=True`; `docker_action`
  enum-limited; no shell path.
- **Unknown ≠ zero** — `cost_known`/`token_source`/`partial` flags everywhere.
- **Ghost guard** — empty-log/absent-artifact warnings on a `0` exit.

---

## Verification log (second pass, freshly run)

| Check | Command | Result |
|---|---|---|
| Preflight, no-rasteriser interpreter | `python analysis/preflight.py` (@C:\Python314) | **FAIL — no rasteriser** (truthful; cache masking removed) |
| Preflight, venv | venv `python analysis/preflight.py` | `[ok] 80,430 byte PNG` (unique probe name) |
| CLI gate | `bench.py run --backend openai ...` under C:\Python314 | refused before warmup, clear message |
| `RemoteDisconnected` hierarchy | `issubclass` probe | not `URLError`; `ConnectionError` + `HTTPException` → caught ✅ |
| `BadStatusLine`/`IncompleteRead` | same probe | both `HTTPException` → caught (first pass row was wrong) |
| H1 | mock run with `max_tokens_per_episode: 5` | episode stops, `token_cap_hit: true` |
| H2/M5 | crash-mid-run E2E | status `error`, partial summary, report 200, excluded ✅ |
| Summary status | mock nested run | `status: "success"` (was `unknown` — fixed) |
| .env loading | `catalog.resolve_key` → `DSC.load_dotenv()` | loads `configs/.env` (first-pass exclusion #1 stands) |
| Dead code | grep `.reap(` | only the definition + the new `list()` call |
| Failed-run presence | `ls results/agnes-3.0-flash/` | absent → Incident B numbers historical |
| Version consistency | pyproject / `__init__` / server | 2.9.0 across the board |
| Suites | vision-wiring, model-aware 35, failure-recovery 25, lifecycle, hosted+accounting, live-window 38, control-centre 156, vision-probe 36, dashboard JS, dashboard-browser 44, check_docker | **all green** |
| Working tree | `git status --short` | only modified sources + new DEBUG.md (untracked) |

---

## Incident A remediation (operator action)

Use the venv for real runs, or give the default interpreter a rasteriser:

```
C:\Users\naman\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe bench.py run ...
# or
C:\Python314\python.exe -m pip install cairosvg
```

Verify with `python analysis/preflight.py` (offline checks only). Nothing else
is blocking: every defect found in the audits is either fixed here, was never a
defect (M1, the BadStatusLine row), or is historical (Incident B numbers).
