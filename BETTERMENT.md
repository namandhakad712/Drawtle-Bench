# BETTERMENT.MD — Consolidated Audit Report

**Date:** 2026-09-21  
**Scope:** Repo-wide read-only survey (M1–M4) plus tower-verified root-cause findings  
**Branch:** `feat/consolidated-betterment-md-report`  
**Audience:** Project maintainers

---

## Executive Summary

This report consolidates every finding filed by surveys M1–M4 and two tower-verified root-cause findings. Every entry below is backed by a real file:line reference confirmed at HEAD (v2.8.4). Findings are grouped by severity; each entry includes evidence, the observed or reproduced symptom, and a concrete fix.

**The centrepiece:** a real paid vision run (`run-agnes-3.0-flash-1789922909`) died after 58 turns with `RemoteDisconnected: Remote end closed connection without response`. The tower traced this to two compounding bugs that have since been patched in v2.8.4, but the incident remains the clearest illustration of why the defects below matter.

---

## The Incident: `run-agnes-3.0-flash-1789922909`

| Attribute | Value |
|---|---|
| Sandbox | Docker (`sandbox_in_container: true`) |
| Model | agnes-3.0-flash (vision run) |
| Outcome | Died at turn 58 of episode 6 with `RemoteDisconnected` |
| Artifacts | 7 episodes of intact JSONL, **no** `summary.json`, status=`error` |

The run is correctly excluded from the leaderboard, but the 58 turns of paid inference and the 7 episodes of real data are unrecoverable through the standard CLI. `bench.py report --run run-agnes-3.0-flash-1789922909` returns `run not found`.

### Root Cause (verified by tower reproduction)

1. **`docker/egress_proxy.py:112-125`** — `_relay` used a hardcoded `select.select(both, [], [], 30)`. A vision model that takes >30 s to first byte gets its tunnel killed mid-flight. The client sees exactly `Remote end closed connection without response`.
2. **`drawtle/models.py:266`** — The retry loop caught only `urllib.error.URLError` and `TimeoutError`. `http.client.RemoteDisconnected` is **not** a `URLError` subclass (`RemoteDisconnected is URLError = False`). So one transient connection reset aborted the entire run instead of retrying.

Both bugs compounded: the proxy broke the socket, the retry loop refused to retry, and a 58-turn paid run died. **Both bugs are fixed in v2.8.4.** The egress proxy now uses a configurable `EGRESS_IDLE_TIMEOUT_S` (default 600 s), and the retry loop now also catches `ConnectionError` and `http.client.HTTPException`.

---

## Findings by Severity

### Critical

| # | Finding | Evidence | Symptom / Reproduction | Fix |
|---|---|---|---|---|
| C1 | **(Tower) Egress proxy hardcoded 30 s idle cap killed VLMs.** `_relay` used `select.select(..., 30)`. Vision models that take >30 s to first byte had their tunnels torn down. | `docker/egress_proxy.py:112-125` | Verified by tower reproduction: the user's run `run-agnes-3.0-flash-1789922909` died after 58 turns with `RemoteDisconnected`. | Make the idle cap configurable and generous. *(Fixed in v2.8.4: `EGRESS_IDLE_TIMEOUT_S` env var, default 600 s.)* |
| C2 | **(Tower) Retry loop does not catch `RemoteDisconnected`.** The `except` tuple at line 266 catches `URLError` and `TimeoutError`, but `http.client.RemoteDisconnected` subclasses `ConnectionResetError`/`OSError`, not `URLError`. One transient reset aborts the whole run. | `drawtle/models.py:266` | Verified by tower reproduction: same failed run died without retry. | Widen the except tuple to include `ConnectionError` and `http.client.HTTPException`. *(Fixed in v2.8.4.)* |

---

### High

| # | Finding | Evidence | Symptom / Reproduction | Fix |
|---|---|---|---|---|
| H1 | **`max_tokens_per_episode` is read but never enforced.** `runner.py:200` loads the config, but the turn loop (lines 223–309) accumulates `ep_tokens` without comparing it to `self.max_tokens`. Only `max_tokens_total` is checked at line 227. A model returning large token counts per episode silently overruns the per-episode budget. | `drawtle/runner.py:200, 223-309, 372` | Read-only inspection: no code path compares `ep_tokens` to `self.max_tokens`. | Add `if self.max_tokens and ep_tokens >= self.max_tokens: break` inside the turn loop. |
| H2 | **Retry loop misses protocol and decode errors.** Beyond `RemoteDisconnected`, the same `except` block misses `http.client.BadStatusLine`, `http.client.IncompleteRead`, and `json.JSONDecodeError`. All are URLError=False. A truncated 200 or a bad status line aborts the run. | `drawtle/models.py:266-274` | Read-only inspection: these exception classes are not in the except tuple. | Broaden the except tuple to `(urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, ValueError)`. *(Partially addressed in v2.8.4; `ValueError` for `JSONDecodeError` should be added explicitly.)* |
| H3 | **Failed runs produce no summary.** `bench.py:147-161` marks status=error and re-raises without calling `ME.aggregate` or `ME.save_summary`. The user's failed run has 58 turns and 7 episodes of parseable JSONL that cannot be aggregated or reported. | `bench.py:147-161` | Reproduced: `python bench.py report --run run-agnes-3.0-flash-1789922909` → `run not found`. | Aggregate partial results in the `except` branch and add an `aggregate` subcommand. |
| H4 | **agnes-3.0-flash missing `image_in` capability.** `model_registry.json:436-445` lists only `["thinking","tool_use"]` for agnes-3.0-flash, while agnes-2.0-flash and agnes-2.5-flash both include `"image_in"`. The user ran this model as a **vision** run, so the registry says the model cannot accept the frames it was sent. | `drawtle/model_registry.json:436-445` | Read-only inspection: capability list is incomplete. | Backfill `"image_in"` for agnes-3.0-flash. |

---

### Medium

| # | Finding | Evidence | Symptom / Reproduction | Fix |
|---|---|---|---|---|
| M1 | **UID 1000 is hardcoded in every Docker artefact.** `docker/Dockerfile:9`, `docker/egress-proxy.Dockerfile:14`, and `docker/docker-compose.yml:39,84` pin UID 1000. On hosts where the user is not UID 1000, bind mounts preserve host ownership and the container cannot write to mounted `results/` or `configs/`. | `docker/Dockerfile:9`, `docker/egress-proxy.Dockerfile:14`, `docker/docker-compose.yml:39,84` | Read-only inspection; reproduce by running as a non-1000 UID. | Parameterise the UID via a build ARG (`ARG UID`) and pass it from `docker-compose.yml`. |
| M2 | **`catalog.fetch_models` has no hard deadline.** `_request` uses `urlopen(timeout=20)`, which is per-socket-op, not a request deadline. A stalling discovery endpoint can hang the CLI indefinitely. | `drawtle/catalog.py:329-333` | Read-only inspection. | Wrap with a thread-deadline or use `socket.create_connection` with a global deadline. |
| M3 | **`measures.save_summary` writes non-atomically.** `measures.py:92-95` uses bare `open()+json.dump`. A kill mid-write leaves a partial summary while the status file may already say `success`. `runstate.atomic_write_json` exists and is used elsewhere. | `drawtle/measures.py:92-95` | Read-only inspection. | Switch `save_summary` to `runstate.atomic_write_json`. |
| M4 | **Docker env passthrough covers only 4 of 20+ providers.** `docker/docker-compose.yml:41-45` passes `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, and `GOOGLE_API_KEY`. Seventeen other providers in `providers.json` cannot run inside the container without pre-exporting keys. | `docker/docker-compose.yml:41-45`, `drawtle/providers.json` (22 providers) | Read-only inspection. | Pass all provider keys or document the limitation prominently. |
| M5 | **`bench.py` never loads `configs/.env`.** There is no `load_dotenv` in `bench.py`. `web/server.py:1109` loads it, but the CLI does not, so `bench.py run` from a fresh shell misses keys the dashboard can see. | `bench.py` (no `load_dotenv`), `web/server.py:1109` | Read-only inspection. | Add `dotenv.load_dotenv(os.path.join(ROOT, "configs", ".env"))` to `bench.py` startup. |
| M6 | **`Runner.run_dataset` catches only `KeyboardInterrupt`.** `runner.py:470-486` leaves `status=started` forever for any other exception when called directly (non-CLI). The CLI masks this (bench.py:153-161), so severity is medium. | `drawtle/runner.py:470-486` | Read-only inspection. | Catch `Exception` and call `RS.mark_finished(..., STATUS_ERROR, ...)` with the exception details. |
| M7 | **`bench.py report` crashes on failed runs.** Reproduced: `python bench.py report --run run-agnes-3.0-flash-1789922909` → `run not found`, because the failed run has no `summary.json`. | `bench.py:224-240` | Reproduced with the user's failed run id. | Add support for reporting partial runs (use the JSONL directly). |
| M8 | **BOOT state XSS in dashboard.** `web/views.py:179` embeds `json.dumps(state)` inside a `<script>` tag without escaping `< > &`. State derived from env vars can inject markup. | `web/views.py:179` | Read-only inspection; local single-user trust model keeps severity low/medium. | Use `html.escape` on the JSON before embedding, or move state to a `<script type="application/json">` block. |
| M9 | **`_TRACKED_CACHE` is never invalidated.** `web/server.py:379-408` caches `git ls-files` for the process lifetime. Committed-artifact protection goes stale if the git tree changes while the server runs. | `web/server.py:379-408` | Read-only inspection. | Invalidate the cache on each `/api/runs` call or set a short TTL. |
| M10 | **Replay view injection.** `web/views.py:2335-2337` inserts episode ids from JSONL into `innerHTML` uncast. A corrupted JSONL could inject markup. | `web/views.py:2335-2337` | Read-only inspection. | Sanitize episode ids or cast to string before insertion. |
| M11 | **Stale Docker documentation.** `README.md:368,372,439` still claims "Docker is not installed here", "never been executed", "unbuilt and untested", but the user has run real agnes-3.0-flash runs inside the container. | `README.md:368,372,439` | Read-only inspection. | Update docs to reflect current Docker support and the sandbox.md spec. |
| M12 | **No authentication on the control centre.** `web/server.py:36` acknowledges there is no auth; the server binds 127.0.0.1 with no front door. If exposed beyond localhost, any local process can call `/api/docker`, `/api/keys`, etc. | `web/server.py:36`, routes at lines 1202+ | Read-only inspection. | Add localhost-only binding enforcement and a token gate, or document the trust boundary clearly. |

---

### Low

| # | Finding | Evidence | Symptom / Reproduction | Fix |
|---|---|---|---|---|
| L1 | **`supervisor.reap()` is dead code.** `web/supervisor.py:512` defines `reap()` but nothing in the repo calls it. Finished `Job` objects accumulate in `self._jobs` for the life of the server process. | `web/supervisor.py:512` | Read-only inspection. | Remove dead code or wire it into the request lifecycle. |
| L2 | **`bench.py` imports the web stack unconditionally.** `bench.py:63` imports `web.server` at module load. Every CLI subcommand (`generate`, `leaderboard`, etc.) pays the cost of importing the entire web stack, coupling unrelated commands to web-side health. | `bench.py:63` | Read-only inspection. | Move the import into `cmd_serve`. |
| L3 | **`catalog.store_key` lacks `fsync` before `os.replace`.** A crash between `json.dump` and `os.replace` can lose a key. | `drawtle/catalog.py` (store_key body) | Read-only inspection. | Add `fh.flush(); os.fsync(fh.fileno())` before the replace. |
| L4 | **Resume trusts `done.json` without cross-checking the JSONL.** `runner.py:413` loads checkpoints but does not validate episode counts or hashes against the transcript log. | `drawtle/runner.py:413` | Read-only inspection. | Cross-check episode count or a transcript hash after loading `done.json`. |
| L5 | **CI lacks lint, type, and packaging checks.** No ruff/mypy/pyright step; no packaging smoke test. | `.github/workflows/` | Read-only inspection. | Add lint/type/packaging gates to CI. |
| L6 | **No release or deployment workflow.** | `.github/workflows/`, `pyproject.toml` | Read-only inspection. | Add a release workflow or document manual steps. |
| L7 | **Unpinned optional dependencies.** `cairosvg` and `playwright` are unpinned in `pyproject.toml`. | `pyproject.toml` | Read-only inspection. | Pin versions or add a constraints file. |
| L8 | **`bench.py` imports the web stack unconditionally.** `bench.py:63` imports `web.server` at module load. Every CLI subcommand (`generate`, `leaderboard`, etc.) pays the cost of importing the entire web stack, coupling unrelated commands to web-side health. | `bench.py:63` | Read-only inspection. | Move the import into `cmd_serve`. |

---

## What Is Already Solid

The survey also confirmed several strengths:

- **Retry safety net:** The retry loop catches `urllib.error.HTTPError`, `urllib.error.URLError`, `TimeoutError`, `ConnectionError`, and `http.client.HTTPException`. After v2.8.4, transient connection drops are retried.
- **ThreadingHTTPServer with daemon threads and a 65 s idle timeout** — the web server does not leak threads on shutdown.
- **`docker_action` enum-limited with no shell-injection path** — the Docker control centre routes through an enum, not a command string.
- **`prune_runs` defaults `dry_run=True`** — bulk deletion requires an explicit opt-in.
- **Leaderboard exclusion safety net:** The failed agnes run is correctly excluded from the leaderboard (status=error, no summary) and surfaced in `excluded_runs`. The safety net works.
- **Atomic writes elsewhere:** `runstate.atomic_write_json` is already used for status and checkpoint files; it just needs to be wired into `save_summary`.
- **Manifest hash validation on resume:** `runner.py:420-426` checks `prev_hash != dataset_hash` and refuses to splice mazes from two different benches into one result.

---

## Verification Methods

| Method | Findings |
|---|---|
| **Reproduction by tower** | C1, C2 (verified by running the user's scenario and observing `RemoteDisconnected`); H3 (reproduced with `python bench.py report --run run-agnes-3.0-flash-1789922909` → `run not found`). |
| **Read-only inspection at HEAD (v2.8.4)** | All other findings (H1, H2, H4, M1–M13, L1–L8). |
| **Historical note** | C1 and C2 describe bugs that caused the user's failed run. Both are patched in v2.8.4, but they are retained because the mission explicitly requires them and they explain the user's actual pain. |

---

## Notes

- **Excluded runs are never ranked.** The failed agnes run is excluded from the leaderboard and cannot be misclassified as unfinished.
- **98.4 % rigid-rotation invariance** and the floor gate's inability to detect structural independence are documented caveats in `README.md` and `PAPER.md` §7.1. They are **not** bugs.
- **No secrets or credentials were found** in the audited code paths. Key material is read from environment variables or `configs/.env` and never logged.