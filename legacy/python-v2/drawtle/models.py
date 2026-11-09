"""Model backends for Drawtle Bench.

A backend is the ONLY channel between the bench and a language model. The model
never gets filesystem, shell, or network access -- it receives messages and
returns text. That is the isolation contract: the "sandbox" is the protocol.

Transport: every provider that speaks the OpenAI chat-completions wire format is
driven by the `openai` SDK when it is installed (pooled connections, honest
`usage` objects including cached/reasoning token breakdowns). The stdlib urllib
path remains as the zero-dependency fallback and is byte-equivalent in body
shape, so a fresh checkout runs before `pip install -r requirements.txt`.
Anthropic keeps its own protocol via urllib (it is not OpenAI-compatible).
Real calls need an API key in the environment; everything else degrades to a
clear error.

Best-practice features wired in here:
  - exponential-backoff retries with honouring Retry-After (429/5xx)
  - per-request timeout under a hard wall-clock deadline
  - token + cost tracking (from a price table; overridden by API usage when given)
  - reasoning split from the answer (`reasoning` field, never parsed)
  - a MockBackend so the whole pipeline is testable with no network/key
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field

try:
    from . import catalog as CAT
except ImportError:                                     # direct script use
    import catalog as CAT                               # type: ignore


@dataclass
class ModelResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    #: False when no source-backed price was available for this model, so a
    #: 0.0 in cost_usd means "unknown", not "free".
    cost_known: bool = True
    #: "measured" when the provider returned a usage block, "estimated" when the
    #: counts were inferred locally from text length. A provider that returns no
    #: usage at all is not a provider whose tokens are unknown -- it is one whose
    #: tokens must be estimated, and the difference has to survive into the log.
    #: Never set this to "measured" for a number we computed ourselves.
    token_source: str = "measured"
    raw: dict = field(default_factory=dict)
    #: The model's reasoning/thinking, SEPARATE from `text` when the provider
    #: returns it in its own field (`reasoning_content`, `reasoning`, or an
    #: Anthropic thinking block). When a provider inlines thinking into
    #: `content` (InternLM with thinking_mode on), the wrapper cannot split it
    #: here -- the backend asks for thinking off and the parser tolerates
    #: leftovers. Never a source of the answer: `text` is what gets parsed.
    reasoning: str = ""
    #: `usage.prompt_tokens_details.cached_tokens` when reported (0 if not).
    cached_tokens: int = 0
    #: `usage.completion_tokens_details.reasoning_tokens` when reported (0 if not).
    reasoning_tokens: int = 0


#: Sentinel for "the provider gave us no usage block". Kept distinct from 0 so a
#: backend can tell "reported zero output tokens" (real, e.g. a filtered reply)
#: from "reported nothing at all" (needs an estimate).
NO_USAGE = object()


# Rough token estimate when the API does not report usage. Cheap and good enough
# for cost caps; replace with tiktoken if installed for accuracy.
def _est_tokens(text):
    return max(1, len(text) // 4)


def _normalize_sdk_error(exc, url):
    """Map an OpenAI-SDK exception onto the urllib-shaped errors the retry loop
    already knows how to handle.

    `complete` decides transience from `urllib.error.HTTPError` (status codes)
    and `URLError`/`TimeoutError`/`ConnectionError` (transport). The SDK raises
    its own hierarchy (`APIStatusError` with `.status_code`, `APIConnectionError`,
    `APITimeoutError`), so without this mapping a 429 or a dropped connection
    from the SDK path would skip the retry logic entirely. HTTP status errors
    carry `Retry-After` on `.headers` when present; preserve it so the 429
    backoff honours the provider.
    """
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        hdrs = {}
        h = getattr(exc, "headers", None)
        if h is not None:
            try:
                ra = h.get("retry-after") or h.get("Retry-After")
            except Exception:                     # noqa: BLE001
                ra = None
            if ra:
                hdrs["Retry-After"] = ra
        return urllib.error.HTTPError(url, code, str(exc), hdrs, None)
    name = type(exc).__name__
    if name in ("APITimeoutError", "APIConnectionError", "ConnectTimeout"):
        return TimeoutError(f"{name}: {exc}")
    if name in ("ConnectionError", "RemoteProtocolError", "ReadTimeout"):
        return ConnectionError(f"{name}: {exc}")
    if name in ("APIError", "InternalServerError", "RateLimitError",
                "APIConnectionError"):
        return urllib.error.URLError(f"{name}: {exc}")
    return exc


def _resolve_usage(usage, prompt_text, completion_text,
                   in_keys=("prompt_tokens", "input_tokens"),
                   out_keys=("completion_tokens", "output_tokens")):
    """Return (prompt_tokens, completion_tokens, token_source) from a usage dict.

    One place decides how a provider's numbers are read, because the previous
    version decided it inline in each backend and got it wrong in a way that
    survived into every committed result: a missing field fell back to
    `_est_tokens(completion_text)` for BOTH counts, so an unmeasured input was
    reported as an estimate of the output.

    Rules, in order:
      1. The provider's own numbers, when present. Either field may be present
         alone -- some providers report output only.
      2. Whatever is still missing is estimated from the corresponding text, and
         the response is then labelled `estimated` -- a partial measurement is
         not a measurement.

    `usage` is accepted as None or {} and both mean "no usage block".
    """
    u = usage or {}
    pin = pout = None
    for k in in_keys:
        if isinstance(u.get(k), int) and u[k] >= 0:
            pin = u[k]
            break
    for k in out_keys:
        if isinstance(u.get(k), int) and u[k] >= 0:
            pout = u[k]
            break
    if pin is not None and pout is not None:
        return pin, pout, "measured"
    if pin is None:
        pin = _est_tokens(prompt_text)
    if pout is None:
        pout = _est_tokens(completion_text)
    return pin, pout, "estimated"


def _usage_details(usage):
    """(cached_input_tokens, reasoning_output_tokens) from a usage dict.

    Both are zero when absent. `cached_tokens` is part of the input count
    (never added on top of prompt_tokens), and `reasoning_tokens` is part of
    the output count -- these are *breakdowns*, reported so a reader can see
    how much of a turn went to thinking rather than to the answer.

    Shape-tolerant: OpenAI spells the breakdown `prompt_tokens_details.
    cached_tokens` / `completion_tokens_details.reasoning_tokens`; Anthropic
    spells it `cache_read_input_tokens` / `cache_creation_input_tokens` and
    `output_tokens_details.reasoning_tokens`; a proxy may spell it `cached`
    at the top level. Read all of them, take the first present.
    """
    u = usage or {}
    cached = None
    for cand in (u.get("cache_read_input_tokens"),
                 u.get("cache_creation_input_tokens"),
                 u.get("cached_tokens"), u.get("cached"),
                 (u.get("prompt_tokens_details") or {}).get("cached_tokens")):
        if isinstance(cand, int) and cand >= 0:
            cached = cand
            break
    reasoning = None
    for cand in ((u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                 (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
                 u.get("reasoning_tokens")):
        if isinstance(cand, int) and cand >= 0:
            reasoning = cand
            break
    return cached or 0, reasoning or 0


# Default price per 1K tokens (USD). Override per model in the run config.
DEFAULT_PRICES = {
    "gpt-4o": {"in": 2.50, "out": 10.00},
    "gpt-4o-mini": {"in": 0.15, "out": 0.60},
    "claude-3-5-sonnet": {"in": 3.00, "out": 15.00},
    "claude-3-5-haiku": {"in": 0.80, "out": 4.00},
    "mock": {"in": 0.0, "out": 0.0},
}


class ModelBackend:
    """Base class. Subclasses implement _post(messages, **kw) -> dict."""

    name = "base"

    #: Environment variables consulted, in order, when no key is passed.
    #: Subclasses override. Empty means the backend needs no key (mock).
    key_env = ()

    def __init__(self, model, api_key=None, prices=None, timeout_s=60.0,
                 max_retries=4, effort=None, **_):
        self.model = model
        self.api_key = api_key
        if self.api_key is None and self.key_env:
            for var in self.key_env:
                self.api_key = os.environ.get(var)
                if self.api_key:
                    break
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.effort = effort
        # Capability metadata, from the local table. Resolved BEFORE pricing,
        # because the table is the price source too -- `DEFAULT_PRICES` below is
        # a legacy fallback kept only for models the table does not cover.
        self.info = CAT.model_info(model) if CAT else {"known": False}
        self.context_window = self.info.get("context_window")
        self.max_output = self.info.get("max_output")

        self.prices = dict(DEFAULT_PRICES)
        if prices:
            self.prices.update(prices)
        # Price precedence: an explicit argument, then the local table (which
        # records where each number came from), then the legacy dict.
        self.price = self.prices.get(model)
        if self.price is None:
            for k, v in self.prices.items():
                if model.startswith(k):
                    self.price = v
                    break
        if not prices and self.info.get("price_in") is not None \
                and self.info.get("price_out") is not None:
            self.price = {"in": float(self.info["price_in"]),
                          "out": float(self.info["price_out"])}
        if self.price is None:
            self.price = {"in": 0.0, "out": 0.0}
        # `priced` records whether the price above is source-backed. `_cost()`
        # reads it, because reporting a fabricated 0.0 for an unknown paid model
        # silently under-reports spend. A model that is genuinely free is a
        # different case from one we have no price for, and is declared as such
        # in the registry with `price_known: true`.
        self.price_known = bool(self.info.get("price_known", False))
        self.priced = (self.price.get("in", 0) or self.price.get("out", 0)) > 0
        # `cost_known` mirrors the above and is set by `_cost()` per call; it is
        # initialised here so the attribute exists on a backend that has not
        # answered anything yet -- a reader should not have to know that the
        # field is a side effect of accounting.
        self.cost_known = self.price_known or self.priced
        self.effort_field, self.effort_levels = (
            CAT.effort_supported(self.name, model) if CAT else (None, ()))
        if effort and not self.effort_field:
            sys.stderr.write(
                f"note: {self.name}/{model} accepts no reasoning-effort control; "
                f"ignoring effort={effort!r}\n")
            self.effort = None
        elif effort and effort not in self.effort_levels:
            raise SystemExit(
                f"{model} accepts reasoning effort in "
                f"{list(self.effort_levels)}, not {effort!r}")

    def check_budget(self, prompt_chars, image_count=0):
        """Warn if the request looks too large for the model's context window.

        A warning, not an error: the estimate is approximate, and refusing a
        run on an estimate would be worse than letting the provider decide.
        The reason to do it at all is that context is the one limit this bench
        is guaranteed to stress -- every prior frame is re-sent every turn.
        """
        if not self.context_window:
            return None
        est = CAT.estimate_request_tokens(prompt_chars, image_count)
        if est > self.context_window * 0.9:
            msg = (f"request ~{est:,} tokens vs a {self.context_window:,} window "
                   f"for {self.model}")
            sys.stderr.write(f"warning: {msg}\n")
            return msg
        return None

    def require_key(self):
        """Raise a legible error if this backend needs a key and has none.

        Without this the request goes out with `Authorization: Bearer None`
        and the provider answers 400/401, so the failure surfaces as an opaque
        HTTP error from deep inside urllib -- which reads as a bug in the
        request body rather than as a missing credential. Called at the start
        of `complete()`, before any network work.
        """
        if self.key_env and not self.api_key:
            raise SystemExit(
                f"backend {self.name!r} needs an API key and none is set.\n"
                f"  looked for: {', '.join(self.key_env)}\n"
                f"  set one, e.g.:  export {self.key_env[0]}=...\n"
                f"  or check the setup first:  python analysis/preflight.py "
                f"--backend {self.name}")

    def _post_with_deadline(self, messages, **kw):
        """Run `_post` under a hard wall-clock deadline.

        urllib's `timeout` is per-socket-operation, not a request deadline: a
        provider that completes the TLS handshake and then trickles the request
        body one byte at a time (or stalls mid-send and never closes the
        socket) keeps every individual send under the limit, so `urlopen` never
        raises and the run hangs forever. That is not hypothetical -- InternLM's
        endpoint was observed doing exactly this, killing an entire run at turn
        zero with no error, no log line and no progress.

        The fix is a real deadline. `_post` runs in a worker thread; if it does
        not return within `timeout_s` the main thread raises `TimeoutError`,
        which `complete`'s retry loop already handles like any other transient
        failure. The worker is a daemon, so a genuinely stuck socket dies with
        the process instead of blocking exit.
        """
        import threading
        box = {}
        def _run():
            try:
                box["ok"] = self._post(messages, **kw)
            except BaseException as e:        # noqa: BLE001 - reported to caller
                box["err"] = e
        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(self.timeout_s)
        if "ok" in box:
            return box["ok"]
        if "err" in box:
            raise box["err"]
        raise TimeoutError(
            f"{self.name}: request did not finish within {self.timeout_s:.0f}s "
            f"(the provider stalled after connecting; the socket is abandoned).")

    def complete(self, messages, temperature=0.0, max_tokens=256, **kw):
        """Call the model with retries. Returns ModelResponse.

        The transient set includes `ConnectionError` and `http.client.
        HTTPException` on purpose. `RemoteDisconnected` -- the peer closed the
        connection without a response -- is both of those but is NOT a
        `urllib.error.URLError`, so the old filter let it straight through and
        one proxy hiccup terminated a whole live run at turn 58 of episode 6,
        after real time and tokens were already spent. A dropped connection is
        the textbook transient fault; a genuinely dead endpoint still fails
        after `max_retries` and surfaces the last error.
        """
        import http.client
        self.require_key()
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                t0 = time.time()
                payload = self._post_with_deadline(
                    messages, temperature=temperature,
                    max_tokens=max_tokens, **kw)
                lat = time.time() - t0
                return self._wrap(payload, lat)
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429 or e.code >= 500:
                    retry_after = e.headers.get("Retry-After")
                    wait = float(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** attempt)
                    if attempt < self.max_retries:
                        time.sleep(min(wait, 30))
                        continue
                raise
            except (urllib.error.URLError, TimeoutError,
                    ConnectionError, http.client.HTTPException) as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise
        raise RuntimeError(f"backend {self.name} failed: {last_err}")

    def _wrap(self, payload, lat):
        raise NotImplementedError

    def _cost(self, pin, pout):
        # A price is usable if it is source-backed (`price_known`) or non-zero.
        # A genuinely free model has price_known=true and a real 0.0; a model
        # with no entry at all has neither, and its 0.0 must not be read as
        # "this call was free".
        if not (self.price_known or self.priced):
            self.cost_known = False
            return 0.0
        self.cost_known = True
        return (pin / 1000.0) * self.price["in"] + (pout / 1000.0) * self.price["out"]

    # ------------------------------------------------------ SDK transport ---
    #
    # One transport for every provider that speaks the OpenAI chat-completions
    # wire format, chosen at request time:
    #   - `openai` SDK when it is installed (the recommended transport: pooled
    #     connections, SDK-level retry fields, honest usage objects including
    #     cached/reasoning breakdowns). It honours HTTP_PROXY/HTTPS_PROXY, so
    #     the egress-proxy sandbox keeps working unchanged.
    #   - stdlib urllib when it is not (zero-dependency fallback, so the bench
    #     still runs on a fresh checkout and in tests).
    # The hand-written retry/deadline wrapper stays the ONLY policy for both
    # paths -- the SDK's own retries are disabled (max_retries=0) so a 429 is
    # handled exactly once, with the same backoff, whether it came from the
    # SDK path or the urllib path.

    def _sdk_client(self, url):
        """An OpenAI-compatible client for `url`, cached per URL. None if no SDK.

        `url` is the FULL chat-completions endpoint (providers.json stores
        `https://host/v1/chat/completions`), but the SDK treats its `base_url`
        as the origin+prefix and appends `/chat/completions` itself -- passing
        the full URL made every SDK call hit `.../chat/completions/chat/
        completions`, which is a plain 404 from every provider. The SDK base is
        therefore the endpoint without that suffix; the urllib path below
        continues to use the full URL unmodified.
        """
        cache = getattr(self, "_sdk_clients", None)
        if cache is None:
            cache = {}
            self._sdk_clients = cache
        if url in cache:
            return cache[url]
        try:
            from openai import OpenAI
        except ImportError:
            if not getattr(self, "_sdk_warned", False):
                self._sdk_warned = True
                sys.stderr.write(
                    f"note: openai SDK not installed; using the stdlib "
                    f"transport for {self.name} (pip install -r "
                    f"requirements.txt for the supported path)\n")
            return None
        suffix = "/chat/completions"
        sdk_base = url[:-len(suffix)] if url.endswith(suffix) else url
        client = OpenAI(base_url=sdk_base, api_key=self.api_key or "none",
                        timeout=self.timeout_s, max_retries=0)
        cache[url] = client
        return client

    def _openai_compat_post(self, url, messages, temperature, max_tokens,
                            body_extra=None):
        """POST an OpenAI-shaped request. Returns the parsed payload dict.

        `body_extra` carries the provider-specific knobs (reasoning effort,
        thinking_mode, response_format) that live in providers.json. The SDK
        path sends the standard fields as first-class arguments and everything
        else as `extra_body`, because the SDK rejects unknown keyword
        arguments; the urllib path merges them into the body directly.
        """
        body = {"model": self.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens}
        if body_extra:
            body.update(body_extra)
        self._last_prompt_text = "\n".join(
            str(m.get("content", "")) for m in messages)

        client = self._sdk_client(url)
        if client is not None:
            # Standard fields as first-class arguments; provider knobs via
            # `extra_body` (the SDK rejects unknown keyword arguments like
            # thinking_mode, but passes them through inside extra_body).
            client_kw = {"model": self.model, "messages": messages,
                         "temperature": temperature, "max_tokens": max_tokens}
            extra = {k: v for k, v in body.items() if k not in client_kw}
            if extra:
                client_kw["extra_body"] = extra
            try:
                resp = client.chat.completions.create(**client_kw)
                # SDK objects are pydantic; `model_dump()` gives the plain
                # dict shape `_wrap` expects (choices/usage/...).
                return resp.model_dump()
            except BaseException as exc:          # noqa: BLE001 - normalise
                raise _normalize_sdk_error(exc, url) from exc

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read().decode())


class MockBackend(ModelBackend):
    """Deterministic backend for pipeline tests. No network.

    mode="optimal" answers the current maze; mode="stale" answers from `lag`
    turns ago (the failure mode the bench targets). It reads the turn index and
    rotation schedule out of the last user message, which the LLMPolicy stamps
    there for exactly this purpose.
    """

    name = "mock"

    def __init__(self, model="mock", mode="optimal", lag=1, seed=0, **_):
        super().__init__(model)
        self.mode = mode
        self.lag = lag
        self.rng = __import__("random").Random(seed)
        self._history = []          # (rotation_deg, optimal_action_json)
        self._turn = 0

    def _post(self, messages, **kw):
        # The policy stamps the observation as JSON in the last user message.
        last = messages[-1]["content"]
        if isinstance(last, list):
            last = " ".join(p.get("text", "") for p in last if isinstance(p, dict))
        try:
            obs = json.loads(last[last.index("{"): last.rindex("}") + 1])
        except Exception:
            obs = {}
        deg = obs.get("rotation_deg", 0)
        true_heading = obs.get("true_heading", 0)
        true_action = obs.get("optimal_action")   # dict {turn, step} from current maze
        cur = {"turn": true_action["turn"] if true_action else 0,
               "step": true_action["step"] if true_action else 1}
        self._history.append((deg, cur))
        if self.mode == "optimal":
            out = cur
        else:
            src = self._history[max(0, len(self._history) - 1 - self.lag)]
            out = src[1]
        self._turn += 1
        text = json.dumps(out)
        # simulate small token usage
        return {"choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": _est_tokens(last),
                          "completion_tokens": _est_tokens(text)}}

    def _wrap(self, payload, lat):
        ch = payload["choices"][0]["message"]["content"]
        u = payload.get("usage") or {}
        # The mock reports its own synthetic counts, so it is a "measured"
        # backend by this rule. That is the point: a run against the mock must
        # exercise the same measured path a real provider takes, or the metric
        # wiring is untested until the first paid call.
        pin = u.get("prompt_tokens")
        pout = u.get("completion_tokens")
        src = "measured" if (pin is not None and pout is not None) else "estimated"
        if pin is None:
            pin = _est_tokens(ch)
        if pout is None:
            pout = _est_tokens(ch)
        return ModelResponse(text=ch, prompt_tokens=pin, completion_tokens=pout,
                             cost_usd=self._cost(pin, pout), latency_s=lat,
                             token_source=src)


class OpenAIBackend(ModelBackend):
    name = "openai"
    BASE = "https://api.openai.com/v1/chat/completions"
    key_env = ("OPENAI_API_KEY",)

    def __init__(self, model="gpt-4o", api_key=None, **kw):
        super().__init__(model, api_key=api_key, **kw)

    def _spec_params(self):
        """Registry request knobs for THIS backend name, or {}.

        `request_params` and `json_mode` belong to providers.json so a
        provider stays declarative; the hand-written OpenAI/Gemini classes read
        them too (via `spec = provider_spec(self.name)`) rather than each
        subclass restating them.
        """
        spec = provider_spec(self.name) if self.name != "base" else None
        return (spec or {})

    def _post(self, messages, temperature=0.0, max_tokens=256, **kw):
        # Registry knobs ride in providers.json, not in this class: reasoning
        # effort when known, plus `request_params`/`json_mode` from the spec.
        extra = {}
        if self.effort and self.effort_field:
            extra[self.effort_field] = self.effort
        spec = self._spec_params()
        extra.update(spec.get("request_params") or {})
        if spec.get("json_mode"):
            extra["response_format"] = {"type": "json_object"}
        # The image is already in `messages`: the policy assembles the request
        # in OpenAI's multimodal shape, so the frame travels as part of the
        # conversation. There used to be an `image_b64` parameter here that
        # nothing ever passed -- a second, dead way to inject an image that a
        # reader would reasonably assume was doing something. Removed rather
        # than left as a trap.
        return self._openai_compat_post(
            self.BASE, messages, temperature, max_tokens, extra)

    def _wrap(self, payload, lat):
        ch = payload["choices"][0]["message"]["content"] or ""
        # Prompt text for the estimator: the request body, not the response.
        prompt_text = getattr(self, "_last_prompt_text", "")
        pin, pout, src = _resolve_usage(payload.get("usage"), prompt_text, ch)
        cached, rtoks = _usage_details(payload.get("usage"))
        return ModelResponse(text=ch, prompt_tokens=pin, completion_tokens=pout,
                             cost_usd=self._cost(pin, pout), latency_s=lat,
                             token_source=src, raw=payload,
                             cached_tokens=cached, reasoning_tokens=rtoks)


def _anthropic_block(block):
    """Translate one content block to Anthropic's Messages-API shape.

    The harness assembles requests in the OpenAI multimodal spelling
    (LLMPolicy.act); Anthropic does not accept `image_url`. A data URI splits
    into the media type and the payload, which is all the conversion is.

    Unrecognised blocks pass through unchanged: this is a translation of what
    the bench actually sends, not a general validator, and a block the harness
    never emits costs nothing to check here.
    """
    if not isinstance(block, dict) or block.get("type") != "image_url":
        return block
    url = (block.get("image_url") or {}).get("url") or ""
    if not isinstance(url, str) or "," not in url:
        return block
    head, _, data = url.partition(",")
    media = head.split(";")[0].split(":", 1)[-1] or "image/png"
    return {"type": "image", "source": {"type": "base64",
                                        "media_type": media, "data": data}}


class AnthropicBackend(ModelBackend):
    name = "anthropic"
    BASE = "https://api.anthropic.com/v1/messages"
    key_env = ("ANTHROPIC_API_KEY",)

    def __init__(self, model="claude-3-5-sonnet", api_key=None, **kw):
        super().__init__(model, api_key=api_key, **kw)
    def _post(self, messages, temperature=0.0, max_tokens=256, **kw):
        # Map openai-style messages to anthropic (system separate).
        sys_text = ""
        turns = []
        for m in messages:
            if m["role"] == "system":
                sys_text += m["content"] + "\n"
            else:
                turns.append({"role": m["role"], "content": m["content"]})
        body = {"model": self.model, "max_tokens": max_tokens,
                "system": sys_text.strip(), "messages": turns}
        # anthropic wants structured content blocks; rebuild the turns as
        # blocks. The harness sends OpenAI's multimodal spelling
        # ({"type": "image_url", "image_url": {"url": "data:...;base64,..."}})
        # -- see LLMPolicy.act -- which Anthropic's Messages API does not
        # accept, so an image block is translated here rather than passed
        # through. Anything the harness does not emit is left untouched.
        body["messages"] = [
            {"role": t["role"], "content": [
                {"type": "text", "text": t["content"]}]}
            if not isinstance(t["content"], list)
            else {"role": t["role"], "content": [_anthropic_block(b)
                                                 for b in t["content"]]}
            for t in turns]
        self._last_prompt_text = sys_text + "\n".join(
            str(t.get("content", "")) for t in turns)
        req = urllib.request.Request(
            self.BASE, data=json.dumps(body).encode(),
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                      "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read().decode())

    def _wrap(self, payload, lat):
        blocks = payload.get("content", []) or []
        txt = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        # Anthropic returns extended thinking as content blocks of type
        # "thinking" -- never merged into `text`, recorded separately.
        reasoning = "".join(
            b.get("thinking", "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "thinking")
        prompt_text = getattr(self, "_last_prompt_text", "")
        pin, pout, src = _resolve_usage(payload.get("usage"), prompt_text, txt,
                                        in_keys=("input_tokens", "prompt_tokens"),
                                        out_keys=("output_tokens", "completion_tokens"))
        cached, rtoks = _usage_details(payload.get("usage"))
        return ModelResponse(text=txt, prompt_tokens=pin, completion_tokens=pout,
                             cost_usd=self._cost(pin, pout), latency_s=lat,
                             token_source=src, raw=payload,
                             reasoning=reasoning, cached_tokens=cached,
                             reasoning_tokens=rtoks)


class GeminiBackend(OpenAIBackend):
    """Google Gemini, via its OpenAI-compatible endpoint.

    Gemini exposes an OpenAI-shaped chat-completions API, including image input
    as `image_url` with a `data:image/png;base64,...` URI, so the transport is
    identical to OpenAIBackend and we only change the host and the auth header.

    Chosen as the default free provider for this bench because it has a genuine
    free tier with image input, which makes the whole measurement reproducible
    without a credit card. Rate limits are per project (not per key) and are not
    published as fixed numbers -- they are shown per account in AI Studio. Treat
    them as unknown rather than assuming a figure: run `--preflight` and let the
    API tell you.
    """

    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    key_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(self, model="gemini-2.5-flash", api_key=None, **kw):
        super().__init__(model, api_key=api_key or os.environ.get("GEMINI_API_KEY")
                         or os.environ.get("GOOGLE_API_KEY"), **kw)

    # No _post override: the OpenAI-compatible layer accepts the same body and
    # the same `Authorization: Bearer` header. Gemini-specific knobs (thinking
    # config, safety settings) live under `extra_body.google` and are not needed
    # by this bench.


#: Hand-written backends. Everything else is served by GenericOpenAIBackend
#: from the declarative registry in providers.json. A class is only warranted
#: when the transport differs -- not when only the URL does.
_CLASSES = {"mock": MockBackend, "openai": OpenAIBackend,
            "anthropic": AnthropicBackend, "gemini": GeminiBackend}


def providers():
    """The declarative provider registry. Returns {name: spec}."""
    global _PROVIDERS
    if _PROVIDERS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "providers.json")
        try:
            with open(path, encoding="utf-8") as fh:
                _PROVIDERS = json.load(fh).get("providers", {})
        except (OSError, ValueError) as e:
            sys.stderr.write(f"warning: could not read providers.json ({e}); "
                             f"falling back to built-in backends\n")
            _PROVIDERS = {}
    return _PROVIDERS


_PROVIDERS = None


def provider_spec(kind):
    """Spec for one provider, or None."""
    return providers().get(kind)


def known_backends():
    """Every backend name this build can construct, sorted."""
    return sorted(set(_CLASSES) | set(providers()))


class GenericOpenAIBackend(ModelBackend):
    """Any provider that speaks the OpenAI chat-completions wire format.

    Constructed from a `providers.json` entry rather than written by hand,
    because the differences between these providers are data: a URL, a list of
    environment variables, and occasionally a different name for the token
    counts. None of that justifies a class, and a class per provider means the
    twenty-first provider is the one that never gets added.

    The transport is the same as OpenAIBackend, so this inherits it. It
    overrides what actually varies: the endpoint, whether the model goes in the
    path, and which usage keys to read.
    """

    def __init__(self, model, provider, api_key=None, **kw):
        spec = provider_spec(provider)
        if not spec:
            raise ValueError(f"unknown provider {provider!r}")
        if spec.get("protocol") not in ("openai", "mock"):
            raise ValueError(
                f"provider {provider!r} speaks {spec.get('protocol')!r}, which "
                f"the generic OpenAI transport cannot drive")
        self.provider = provider
        self.spec = spec
        self.name = provider
        # A provider-level deadline (providers.json `timeout_s`) beats the
        # 60s default -- aggregators were observed stalling past it while
        # still being alive. An explicit keyword from a caller still wins.
        kw.setdefault("timeout_s", spec.get("timeout_s") or 60.0)
        # A URL containing `{model}` addresses the model in the path. Only
        # substituted when the placeholder is actually there, so a model name
        # with a slash (OpenRouter's `vendor/model`) is never silently rewritten.
        url = spec.get("url") or ""
        self.url = url.replace("{model}", model) if "{model}" in url else url
        # `key_env` comes from the spec, so `require_key` and preflight agree
        # with the registry without either being taught about the provider.
        self.key_env = tuple(spec.get("key_env") or ())
        # `self_hosted` servers commonly accept any key or none; erroring out
        # for a missing one would refuse a run that would have worked.
        self.required_key = bool(self.key_env) and not spec.get("self_hosted")
        if api_key is None and self.key_env:
            api_key = _resolve_provider_key(provider, self.key_env)
        super().__init__(model, api_key=api_key, **kw)

    def require_key(self):
        if self.required_key and not self.api_key:
            raise SystemExit(
                f"provider {self.name!r} needs an API key and none is set.\n"
                f"  looked for: {', '.join(self.key_env)}\n"
                f"  set one, e.g.:  export {self.key_env[0]}=...\n"
                f"  or store it once:  python -m drawtle.catalog set-key "
                f"{self.name}\n"
                f"  or check the setup first:  python analysis/preflight.py "
                f"--backend {self.name}")

    def _post(self, messages, temperature=0.0, max_tokens=256, **kw):
        if not self.url:
            raise SystemExit(
                f"provider {self.name!r} has no endpoint in providers.json")
        extra = {}
        # Reasoning effort, only when this model is known to accept it. Sending
        # the field to a model that does not know it is a 400 from some
        # providers and silently ignored by others, so it is opt-in per model.
        if self.effort and self.effort_field:
            extra[self.effort_field] = self.effort
        # Provider-level request knobs from providers.json (`request_params`).
        # InternLM's s2/s1 families default `thinking_mode` ON, which makes the
        # model reason aloud inside `content` and eat the request's output
        # budget before the JSON -- that is the run killer this bench hit.
        # Declaring the flag lets the registry turn it OFF for providers that
        # accept it, per family, without a class per provider.
        extra.update(self.spec.get("request_params") or {})
        # Forced JSON output: providers that implement the OpenAI
        # `response_format` (and that have been verified to, hence the
        # explicit `json_mode: true` in the spec) get it. Not sent otherwise:
        # an unverified provider that rejects the field answers 400, which is
        # a worse failure than a markdown-wrapped reply the parser can strip.
        if self.spec.get("json_mode"):
            extra["response_format"] = {"type": "json_object"}
        # Messages are passed through unchanged, including multimodal ones. The
        # policy is what assembles a frame into the request, so a message that
        # already carries a list of parts must be sent as-is -- wrapping a list
        # into {"type":"text","text":[...]} is malformed and some providers
        # reject it. There is no `image_b64` parameter here for the reason given
        # on the OpenAI-compatible `_post`: it was a second, dead way to inject
        # an image.
        return self._openai_compat_post(
            self.url, messages, temperature, max_tokens, extra)

    def _wrap(self, payload, lat):
        # Some providers return a `choices` array that is empty or lacks
        # `message` -- a content filter is the usual cause. Surfacing that as a
        # clear error beats an IndexError from inside the parse.
        choices = payload.get("choices") or []
        if not choices:
            err = payload.get("error") or payload
            raise RuntimeError(
                f"{self.name} returned no choices "
                f"({json.dumps(err)[:300]})")
        msg = choices[0].get("message") or {}
        ch = msg.get("content") or ""
        if isinstance(ch, list):        # some compat layers return content blocks
            ch = "".join(b.get("text", "") for b in ch if isinstance(b, dict))
        # Reasoning is read from its OWN field whenever the provider has one
        # (`reasoning_content` = DeepSeek/Qwen/InternLM previews via gateways,
        # `reasoning` = OpenRouter). It is recorded separately and NEVER parsed
        # as the answer. When a provider inlines thinking into `content`, the
        # spec's `request_params` (e.g. thinking_mode: false) is what stops it.
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if isinstance(reasoning, list):
            reasoning = "".join(
                b.get("text", "") or b.get("reasoning", "")
                for b in reasoning if isinstance(b, dict))
        in_keys, out_keys = self._usage_keys()
        prompt_text = getattr(self, "_last_prompt_text", "")
        pin, pout, src = _resolve_usage(payload.get("usage"), prompt_text, ch,
                                        in_keys=in_keys, out_keys=out_keys)
        cached, rtoks = _usage_details(payload.get("usage"))
        return ModelResponse(text=ch, prompt_tokens=pin, completion_tokens=pout,
                             cost_usd=self._cost(pin, pout), latency_s=lat,
                             token_source=src, raw=payload,
                             reasoning=str(reasoning), cached_tokens=cached,
                             reasoning_tokens=rtoks)

    def _usage_keys(self):
        """Which `usage` keys carry the counts for this provider.

        Defaults to the OpenAI names. A provider whose `usage_fields` is null
        has not been verified to return a usage block at all -- in that case the
        OpenAI names are tried anyway, and if the call comes back without them
        `_resolve_usage` labels the counts `estimated`. Guessing wrong here
        degrades to an honest estimate rather than to a wrong measurement.
        """
        uf = self.spec.get("usage_fields")
        if not uf:
            return ("prompt_tokens", "input_tokens"), \
                   ("completion_tokens", "output_tokens")
        return tuple(uf[0]), tuple(uf[1])


def _resolve_provider_key(provider, key_env):
    """Environment first, then the stored credential file. Never the reverse.

    An explicit `export` must always beat a file, or a user cannot override a
    stale stored key without deleting it.
    """
    for var in key_env:
        v = os.environ.get(var)
        if v:
            return v
    for var in key_env:
        v = _stored_credential(provider, var)
        if v:
            return v
    return None


def _stored_credential(provider, var):
    """Read a key from the credential file, or None. Never raises.

    Imported lazily and defensively: the credential store lives in `catalog`,
    which imports nothing from here, so a circular import is possible in
    principle and a missing key must never be the reason a mock run fails.
    """
    try:
        from . import catalog as _CAT
        key, _src = _CAT.resolve_key(provider)
        return key
    except Exception:
        return None


def make_backend(kind, model, **kw):
    """Factory. Prefers a hand-written backend, falls back to the registry."""
    if kind in _CLASSES:
        return _CLASSES[kind](model, **kw)
    if kind in providers():
        return GenericOpenAIBackend(model, kind, **kw)
    raise ValueError(
        f"unknown backend {kind!r}; have {', '.join(known_backends())}")


def backend_key_env(kind):
    """Environment variables a backend reads its key from (empty if none).

    Used by the CLI and by `analysis/preflight.py` so that the list of accepted
    variable names lives in exactly one place. The registry is consulted for
    providers with no hand-written class, which is what keeps a newly added
    provider visible to preflight without touching it.
    """
    cls = _CLASSES.get(kind)
    if cls is not None:
        return tuple(getattr(cls, "key_env", ()))
    spec = provider_spec(kind)
    return tuple(spec.get("key_env") or ()) if spec else ()
