# Execution-core defect report (M2/M1 audit -> M4 fixes)

Scope of the fix mission M4: `drawtle/runner.py`, `drawtle/models.py`,
`drawtle/cost.py`, `drawtle/runstate.py`, `drawtle/transcript.py`,
`drawtle/protocol.py`, `drawtle/catalog.py`, `drawtle/discovery.py`,
`bench.py`, and their tests. Every defect below was confirmed by the audits
(filed in `.tower/comms/findings/`, see the references column) and reproduced
before the fix; every fix has a regression assertion in
`analysis/test_execution_core.py` that was verified to FAIL on the pre-fix
code and PASS after.

**Summary: 12 defects acted on (1 P0, 6 P1, 5 P2), 2 deferred.**

| # | Sev | Module | One line | Status | Test |
|---|-----|--------|----------|--------|------|
| E1 | P0 | runner.py | Rotation schedule not reproducible from its run_id | **FIXED** | E1, E1b |
| E2 | P1 | runner.py | `_SCHED` shared class attribute across Runner instances | **FIXED** | E1 |
| E3 | P1 | runner.py | Probe mode: rotated exit ends the episode with a fake completion | **FIXED** | E3, E3b, E3c |
| E4 | P1 | discovery.py | Modality-split branch double-suffixes capabilities | **FIXED** | E4 |
| E5 | P1 | cost.py | Prompt-growth term cancelled, projection ~24x too low | **FIXED** | E5 |
| E6 | P1 | runstate/runner/bench | Unvalidated run_id escapes the results directory | **FIXED** | E6 |
| E7 | P1 | cost.py / models.py | Priced at run time, UNKNOWN at estimate time | **FIXED** | E7 |
| E8 | P1 | models.py | AnthropicBackend sends OpenAI-shaped image blocks | **FIXED** | E8 |
| E9 | P2 | transcript.py | Pool keys on content only; roles collapse on replay | **FIXED** | E9 |
| E10 | P2 | runner.py | `last_request` shallow-copies a list the same method mutates | **FIXED** | E10 |
| E11 | P2 | models.py | `cost_known` created lazily by `_cost`, not `__init__` | **FIXED** | E11 |
| E12 | P2 | catalog.py | Gap report does not flag "priced but `price_known` unset" | **FIXED** | E7 |
| D1 | P2 | models.py | HTTP-date `Retry-After` not parsed | **DEFERRED** | — |
| D2 | — | models.py | `ModelResponse.cost_known` default True | **DEFERRED** | — |

---

## E1 (P0) — the rotation schedule is not reproducible from its run_id

**Where** `drawtle/runner.py:868-872` (`Runner._schedule`).

The per-turn wall-rotation schedule — which maze the model is shown each turn —
was seeded with `hash(self.run_id)`. Python's `hash()` on `str` is randomised
per process when `PYTHONHASHSEED` is unset, so the same `run_id` produced a
*different* schedule in every process (measured: 37 of 48 turn positions
differed between two processes). A benchmark score is only interpretable
against the exact frame sequence it was measured on, and that sequence was
neither recoverable nor recorded: the input a published number was measured
against was unreproducible.

**Fix.** The seed is now a stable SHA-256 digest of `f"{run_id}:{t}"`, so the
schedule is a pure function of the run_id in any interpreter and any process.
The schedule is also recorded in the run's status file as
`rotation_schedule`, so a result is auditable and replayable from its own
artifacts without re-running.

**Regression tests.** `E1` asserts same-run_id agreement across Runner
instances and across **two separate processes** (the actual P0), that the
values are lattice-legal (0 or a multiple of 90), and that no `_SCHED` class
attribute remains. `E1b` asserts the status file carries the schedule and that
it matches the runner's. Verified FAIL on the pre-fix code (cross-process
disagreement), PASS after.

---

## E2 (P1) — `_SCHED` was a class attribute shared by every Runner

**Where** `drawtle/runner.py:866` (M1-routed finding; compounds E1).

`_SCHED = {}` was declared in the class body, so it was shared mutable state
across every Runner in one process. `_schedule` cached by turn index only, so
a second Runner with a different run_id got cache hits on every `t` and
replayed the first runner's schedule verbatim — within one process the run_id
was ignored entirely. `analysis/test_lifecycle.py` constructs several Runners
in one process, so this is reachable from the repo's own tests.

**Fix.** `self._sched = {}` is now initialised in `__init__`. Fixed together
with E1 (same function, same audit pair).

**Regression test.** `E1` (same block) asserts the schedule differs between
two Runners with different run_ids and that `_sched` is per-instance.

---

## E3 (P1) — probe mode: an exit rotates onto the stationary entry cell and the episode ends with a fake completion

**Where** `drawtle/runner.py:599-601` (fixed cell), `519-528` (the `arrived`
terminal), `684` (`completion=True`). M1-routed, 58 of 200 episodes affected.

In the shipped probe mode (`navigate=False`) the turtle's cell is held at
`maze.entry` for the whole episode while the maze rotates beneath it.
`rotate_walls(deg)` without `keep_openings` rotates the **exits** too, so a
rotated exit could land exactly on the stationary entry cell. The runner
treated that as an arrival: `reached_exit = True`, the episode ended after
1-6 turns, and the summary recorded `completion=True` with an efficiency of
`optimal_len / max(1, 0 steps)` — full credit for a walk that never happened.
`measures.aggregate` then folded those into `completion_rate` and
`mean_efficiency`.

**Fix.**
1. A no-exit-reachable turn in probe mode is no longer an arrival: it is
   recorded as `error_class="no_exit_reachable"` (a no-model-call, zero-token
   turn, same shape as the old `arrived` turn) and the episode continues to
   its full turn budget. Only `navigate` mode ends the episode on this branch,
   where the turtle actually walked onto an exit.
2. `_ep_summary` now reports `completion: None` and `efficiency: None` in
   probe mode. The turtle never moves, so "did the agent reach an exit" is
   not a question that mode can answer. `None` marks the field as not
   applicable rather than as a failed attempt, and `measures.aggregate` /
   `web/server.py` already exclude `None` entries from `completion_rate` /
   `mean_efficiency` instead of counting them as zero.

**Regression tests.** `E3` runs 60 probe episodes and asserts no episode ends
before `max_turns` with `completion=True` and `steps=0`, and that every probe
episode carries `completion is None` / `efficiency is None`. `E3b` asserts the
`no_exit_reachable` label is actually produced (87 such turns over 200
episodes with seed 11). `E3c` asserts navigate mode still completes genuinely:
a completed nav episode has `steps > 0` and a real efficiency. `E3` was
verified to FAIL on the pre-fix code (9 spurious arrivals, completion flags
on every episode).

**Note.** Probe-mode `progress_rate` is unchanged: the audit confirmed the
scoring is arithmetically sound and separates optimal from stale; the defect
was the completion/efficiency semantics, not the progress measure.

---

## E4 (P1) — discovery double-suffixes capability tokens ("image_in_in")

**Where** `drawtle/discovery.py:386-389` (`_published_metrics`).

For a provider that publishes an `input`/`output` modality split rather than a
capability list — the shape OpenRouter-style aggregators use — the branch
emitted `f"{_norm_cap(c)}_in"`, but `_norm_cap("image")` already returns
`image_in`. Every token was malformed and outside `CAPABILITIES`, so a
genuinely multimodal model was reported text-only, written into the registry
overlay verbatim, and its vision turn cap (`runner._context_turn_cap`, which
tests `"image_in" in caps`) was computed as a text-only run.

**Fix.** Drop the redundant suffix: `caps = sorted({_norm_cap(c) for c in inp
if c})`. The sibling capability-list branch was already correct and is
unchanged.

**Regression test.** `E4` asserts no `_in_in` token is emitted, that `image_in`
is produced for a vision model, and that every emitted token is in
`CAPABILITIES` (or the `text` token, which the vocabulary deliberately omits
but every classifier tolerates as a non-`image_in` entry).

---

## E5 (P1) — cost.estimate cancels the prompt-growth term it documents

**Where** `drawtle/cost.py:69-76`.

The docstring correctly says prompt tokens grow because every prior frame is
re-sent each turn, then the formula computed
`growth * (pin_per_turn / max(1.0, turns / 2.0)) * n` — dividing the
per-turn figure by the sequence's *midpoint*, which cancels the growth. A
48-turn episode came out ~24x too low (4116 tokens projected vs 49392
simulated), so the one number this module exists to produce underpriced a
paid run by that factor in the direction that causes spend.

**Fix.** Sum the sequence: `in_tokens = int(growth * pin_per_turn * n)`, where
`growth = turns*(turns+1)/2` and `pin_per_turn` is what turn 1 costs — exactly
the triangular sum the docstring describes. The projection now matches the
simulated linear-growth run to within rounding (49392 both). The basis also
records that the default per-turn input is a **text-only mock** figure (the
mock's usage is `len(text)/4` and carries no image), and that a vision run
adds ~750 tokens/turn per frame — the audit's second-order note, addressed by
labelling rather than by inventing a measured figure.

**Regression test.** `E5` asserts the projection equals the simulated
linear-growth run, that it is not the old understated value, that input grows
super-linearly in turns while output stays linear, and that the basis carries
the text-only caveat.

---

## E6 (P1, security) — an unvalidated run_id escapes the results directory

**Where** `drawtle/runstate.py:176` (`run_paths`), `drawtle/runner.py:347`
(default id), `bench.py:651` (`--run-id`).

A run id containing path separators was accepted on the write path: `run_paths`
joined it into directory names with no validation, so the sidecars landed
outside the results tree entirely and the JSONL sat in a different directory
from its own status/checkpoint/transcript files. The delete path already
validated (`_SAFE_RUN_ID`); the write path did not. The run then became
invisible to every reader — `find_run_dir` only looks for
`results/<model>/<run_id>/` and the flat fallback looks for `results/<run_id>.*`
— an untracked, unremovable artifact that still reads as a completed paid run.
The default id embeds the model name, and the registry deliberately holds
slash-bearing ids ("IFM/K2-Horizon-375B-A23B"), so this needed no operator
input at all.

**Fix.**
- `runstate.validate_run_id(run_id)` is the one place the contract is spelled
  out, returning `(ok, reason)`.
- `run_paths(..., model=...)` — the writer's shape — raises `ValueError` for
  an id failing `_SAFE_RUN_ID`. The **reader** shape (no `model`) stays
  permissive so legacy runs on disk remain readable.
- `Runner.__init__` refuses a bad id at the earliest library entry point, and
  the default id now slugifies the model part (`run-IFM-K2-Horizon-<ts>`), so
  a slash-bearing model id cannot produce a path-bearing run id.
- `bench.py` validates `--run-id` before constructing the Runner, so the
  message names the flag the operator typed.

**Regression test.** `E6` asserts the validator accepts a plain id and rejects
path-bearing, empty, leading-dash and over-long ids; that `Runner.__init__`
and the writer's `run_paths` both refuse; that a safe run still writes its
sidecars under the results tree; that the reader's `run_paths` still resolves
a legacy id; and that the default id slugifies a slash-bearing model id. The
pre-fix reproduction (sidecars two levels above `results/`) is now blocked
with zero leaked files.

---

## E7 (P1) — priced at run time, UNKNOWN at estimate time

**Where** `drawtle/cost.py:84` vs `drawtle/models.py:374-383`.

Four registry entries (gpt-4o, gpt-4o-mini, claude-3-5-sonnet,
claude-3-5-haiku — the legacy `DEFAULT_PRICES` models, i.e. the common case
for anyone running OpenAI or Anthropic) carry a real `price_in`/`price_out`
but no `price_known` field. `cost.estimate` used `price_known AND priced`, so
it reported `cost_known=False` and no dollar figure; `ModelBackend._cost` used
`price_known OR priced`, so it computed a cost and labelled it known. The same
model was quoted UNKNOWN before a run and totalled in dollars after it.

**Fix.** Both: the code and the data. `cost.estimate` now uses the same test
as the per-call path (`price_known` OR a non-zero price), so the two cannot
disagree; and the four legacy registry entries now carry `"price_known":
true` (their source is `DEFAULT_PRICES`, which the table otherwise trusts).
Either alone would suffice; both together remove the disagreement at its
source and keep it from recurring for the next unflagged priced model.

**Regression test.** `E7` reads the shipped registry file directly (bypassing
any local overlay), asserts the legacy entries are flagged, then — with a
legacy-shaped entry injected via `CAT.model_info` — asserts `cost.estimate`
prices it exactly as `ModelBackend._cost` does. Verified: pre-fix, the
estimate path returned `cost_known=False, usd=None` for the same entry the
per-call path priced at 0.0125.

---

## E8 (P1) — AnthropicBackend sends OpenAI-shaped image blocks to Anthropic

**Where** `drawtle/models.py:616-621` (`AnthropicBackend._post`) vs
`drawtle/runner.py:267-270` (the emitter).

The comment claimed "an image, when there is one, already arrives as a block in
the turn". It does — in the **OpenAI** spelling (`{"type":"image_url",
"image_url":{"url":...}}`), which Anthropic's Messages API does not accept.
Any turn carrying a frame passed through unchanged and would have failed with
a 400 mid-run. The vision path on that backend had never worked and was
covered by no test.

**Fix.** A module-level `_anthropic_block(block)` translates `image_url`
blocks to Anthropic's `{"type":"image","source":{"type":"base64",
"media_type":..., "data":...}}`, splitting the media type and payload off the
data URI. Text and unrecognised blocks pass through untouched — a translation
of what the harness sends, not a general validator. `_post` now maps every
block of a list-content turn through it.

**Regression test.** `E8` unit-tests the helper (png and jpeg media types,
text pass-through, unknown pass-through) and then runs a full
`AnthropicBackend.complete` against a local HTTP stub, asserting the
on-the-wire body: system text separated, a vision turn as a list of blocks,
the image block Anthropic-shaped, and **no `image_url` block surviving**.

---

## E9 (P2) — transcript pool keys on content only, so roles collapse on replay

**Where** `drawtle/transcript.py:71-74` (`content_key`), `100-115` (`add`).

`content_key` hashed the content only; `add` stored the role but only the
first one seen won, so two same-content different-role messages collapsed to
one entry and `replay_transcript` returned the first message's role for both
— an assistant reply replayed as a user message. The docstring promises
byte-identical messages; this turned the audit path into a fabrication path,
silently, and the content hash could not catch it. Dormant on this bench
today (the system prompt is unique, user turns embed "Turn {t}") but real the
moment a fixed reply is reused.

**Fix.** `content_key(content, role=None)` now includes the role in the hashed
blob, and `add` passes `message.get("role")`. Roles are a closed set, so this
cannot split what should be one message.

**Regression test.** `E9` asserts a user/assistant pair with identical
content replays with both roles intact and produces two pool entries; that a
genuinely identical message still pools once with refcount 2; and that roles
survive a serialise/deserialise round trip.

---

## E10 (P2) — `last_request` shallow-copies a list the same method mutates

**Where** `drawtle/runner.py:296` (`LLMPolicy.act`).

`self.last_request = list(self.messages)` copied the list but not the message
dicts inside it. Correct today (nothing edits a message in place), but the
snapshot is the transcript pool's source of truth, and the first in-place edit
— e.g. truncating history to fit a context budget, which the codebase's own
comments anticipate — would silently rewrite the recorded conversation after
the fact.

**Fix.** `self.last_request = [dict(m) for m in self.messages]`. A per-message
copy; `deepcopy` is not needed, payloads are not mutated after construction.

**Regression test.** `E10` asserts the snapshot is populated, is not the live
list, holds copies rather than shared dicts, and is unchanged after appending
to and editing the live list. Verified FAIL on the pre-fix code.

---

## E11 (P2) — `ModelBackend.cost_known` is created lazily by `_cost`

**Where** `drawtle/models.py:374-383` (`_cost`) and `analysis/preflight.py:220`.

`cost_known` did not exist until `_cost()` ran, so a freshly constructed
backend lacked the attribute, and preflight had already papered over it by
re-deriving `price_known OR priced` — reading *different attributes* than the
run path uses, which is exactly the shape that lets the two drift.

**Fix.** `self.cost_known = self.price_known or self.priced` in `__init__`;
`_cost` still sets it per call (unchanged). Preflight now reads
`backend.cost_known` directly — the same attribute the run path uses — with a
`getattr` default only for a pre-3.x backend.

**Regression test.** `E11` asserts the attribute exists before any call, that
a priced backend reports `True` from `__init__`, and that `_cost` does not
contradict the initialised flag.

---

## E12 (P2) — the registry gap report does not flag "priced but unflagged"

**Where** `drawtle/catalog.py:609-640` (`_cmd_price_update`).

The gap report listed models with a missing `price_in` but had nothing to say
about a model that *has* a price and no `price_known` — the state E7 showed is
silent in the one place an operator would look.

**Fix.** `_cmd_price_update` now lists priced-but-unflagged models separately,
with the sentence that they are priced at run time and that adding
`price_known` makes the pre-run estimate agree. No false positives: the test
is a non-zero price with the flag unset.

**Regression test.** covered by E7's direct registry read.

---

## Deferred (with reasons)

**D1 (P2) — HTTP-date `Retry-After` is not parsed.** `drawtle/models.py:330-369`.
The audit verified the retry/backoff policy is **correct as specified** in
every case it exercised: `Retry-After` in seconds is honoured (429 → waits
[5, 5, 5]), clamped at 30s (120 → 30), non-retryable 400 raises immediately,
`RemoteDisconnected` — which is both a `ConnectionError` and an
`HTTPException` but not a `URLError` — **is** retried (a past bug, confirmed
fixed), and the per-request deadline thread prevents a trickle-stall hang. The
one genuine nit is that only the integer-seconds form is parsed, so an
HTTP-date form falls back to `2**attempt` — a safe degradation that backs off
rather than honouring a shorter delay. Deferred because it is a nit that
touches retry timing and the mission brief defers P2 nits in favour of
risk-free changes; the correct fix is a two-line `email.utils` parse with the
30s clamp kept either way.

**D2 — `ModelResponse.cost_known` defaults to `True`.** Related note, same
family as E11. The dataclass default predates the runstate attribute and
reads optimistically for a response built outside `_wrap`. Deferred because
flipping the default is a wire-format change that touches every caller that
constructs a `ModelResponse` by hand (the tests and the stubs do), and E11
removed the actual inconsistency the audit pointed at: the *backend* attribute
that a reader reaches for is now initialised from the real price state. A
default flip should go through its own change with its own coverage.

---

## Verification

Required suites (exact commands, run from the repo root):

```
$ python analysis/test_execution_core.py
========================================================================
227 passed, 0 failed
all execution-core regression checks passed

$ python analysis/test_hosted_and_accounting.py
all accounting and registry tests passed

$ python analysis/test_lifecycle.py
all lifecycle tests passed

$ python analysis/test_model_aware.py
all model-awareness checks passed
```

CI floor check, the exact four steps of `.github/workflows/ci.yml`:

```
$ python bench.py generate --count 20 --sizes 9 --seed 7 --out results/dataset.ci.json
wrote results/dataset.ci.json: 20 mazes, hash sha256:cfbaf396126c0b2d

$ python bench.py run --backend mock --model mock --mode optimal \
      --dataset results/dataset.ci.json --out-dir results --run-id ci-opt --limit 20
... "status": "success", "n_turns": 960

$ python bench.py run --backend mock --model mock --mode stale --lag 1 \
      --dataset results/dataset.ci.json --out-dir results --run-id ci-stale --limit 20
... "status": "success", "n_turns": 960

$ python analysis/ci_assert.py
optimal progress : 1.000
stale   progress : 0.233
PASS: floor check holds (gap 0.767 >= 0.30)
```

Other CI steps run locally and green: `gate_falsification`, `semantics_check`,
`killtest`, `test_datasets`, `test_run_failure_recovery`, `test_live_window`,
`test_view_helpers`, `test_vision_wiring`, `test_probe_endpoint`,
`test_dashboard_smoke`, `check_dashboard_js`, `test_model_aware` (with the
`openai` SDK installed).

**Two environment-blocked results, both pre-existing and unrelated to these
changes** (verified by re-running against the pre-change code):

- `analysis/preflight.py` cannot complete on this machine: no SVG->PNG
  rasteriser is importable in this interpreter (`cairosvg: False,
  playwright: False`; chromium is installed but its Python package is not),
  so preflight stops at step 1. The multimodal rasteriser path is unverified
  here — a green preflight is not a full green. Not a defect this mission can
  fix.
- `analysis/test_control_centre.py` fails one check, "preflight finds the
  key" (expects a stored credential this machine does not have). Fails
  identically before these changes; 165/166 checks pass.

## Honest notes

- `cost.estimate`'s default per-turn input is a **text-only mock** figure
  (42 tokens). The formula is now correct, but a *vision* run still costs
  ~750 tokens/turn of image more than the projection says unless the caller
  passes `prompt_tokens_per_turn` from a measured run. The basis now states
  this explicitly rather than presenting the mock figure as measured.
- The schedule recorded in `status.json` is a *derived* record: it equals
  what `_schedule(t)` returns, but a reader who wants to verify it still
  needs the algorithm and the run_id. Recording it removes the need to trust
  a code version, which is the part that mattered.
- Probe-mode `progress_rate` is unchanged by this fix. The audit confirmed it
  separates optimal from stale soundly; the defect was in what the mode
  claimed about completion, not in what it measured.
