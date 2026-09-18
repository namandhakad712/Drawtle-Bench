"""Model backends for Drawtle Bench.

A backend is the ONLY channel between the bench and a language model. The model
never gets filesystem, shell, or network access -- it receives messages and
returns text. That is the isolation contract: the "sandbox" is the protocol.

Backends are stdlib-only (urllib) so the bench runs without installing SDKs.
Real calls need an API key in the environment; everything else degrades to a
clear error.

Best-practice features wired in here:
  - exponential-backoff retries with honouring Retry-After (429/5xx)
  - per-request timeout
  - token + cost tracking (from a price table; overridden by API usage when given)
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


#: Sentinel for "the provider gave us no usage block". Kept distinct from 0 so a
#: backend can tell "reported zero output tokens" (real, e.g. a filtered reply)
#: from "reported nothing at all" (needs an estimate).
NO_USAGE = object()


# Rough token estimate when the API does not report usage. Cheap and good enough
# for cost caps; replace with tiktoken if installed for accuracy.
def _est_tokens(text):
    return max(1, len(text) // 4)


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
        if not prices and self.info.get("price_in") is not None:
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

    def complete(self, messages, temperature=0.0, max_tokens=256, **kw):
        """Call the model with retries. Returns ModelResponse."""
        self.require_key()
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                t0 = time.time()
                payload = self._post(messages, temperature=temperature,
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
            except (urllib.error.URLError, TimeoutError) as e:
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

    def _post(self, messages, temperature=0.0, max_tokens=256, image_b64=None, **kw):
        content = []
        for m in messages:
            content.append({"type": "text", "text": m["content"]})
        if image_b64:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"}})
        body = {"model": self.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens}
        # Reasoning effort, only when this model is known to accept it. Sending
        # the field to a model that does not know it is a 400 from some
        # providers and silently ignored by others, so it is opt-in per model.
        if self.effort and self.effort_field:
            body[self.effort_field] = self.effort
        # OpenAI wants image as a user message; we put text prompt in `messages`
        # and attach the image to the final user turn.
        if image_b64:
            body["messages"] = list(messages)
            body["messages"][-1] = {"role": "user", "content": content}
        # Keep the text we are about to send, so `_wrap` can estimate tokens
        # against the REQUEST when the provider gives no usage block. Estimating
        # from the response text (the old behaviour) reported the input count as
        # a function of the output -- which is both wrong and, on this bench,
        # wrong in the direction that understates cost.
        self._last_prompt_text = "\n".join(
            str(m.get("content", "")) for m in messages)
        req = urllib.request.Request(
            self.BASE, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                      "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read().decode())

    def _wrap(self, payload, lat):
        ch = payload["choices"][0]["message"]["content"] or ""
        # Prompt text for the estimator: the request body, not the response.
        prompt_text = getattr(self, "_last_prompt_text", "")
        pin, pout, src = _resolve_usage(payload.get("usage"), prompt_text, ch)
        return ModelResponse(text=ch, prompt_tokens=pin, completion_tokens=pout,
                             cost_usd=self._cost(pin, pout), latency_s=lat,
                             token_source=src, raw=payload)


class AnthropicBackend(ModelBackend):
    name = "anthropic"
    BASE = "https://api.anthropic.com/v1/messages"
    key_env = ("ANTHROPIC_API_KEY",)

    def __init__(self, model="claude-3-5-sonnet", api_key=None, **kw):
        super().__init__(model, api_key=api_key, **kw)

    def _post(self, messages, temperature=0.0, max_tokens=256, image_b64=None, **kw):
        # Map openai-style messages to anthropic (system separate).
        sys_text = ""
        turns = []
        for m in messages:
            if m["role"] == "system":
                sys_text += m["content"] + "\n"
            else:
                turns.append({"role": m["role"], "content": m["content"]})
        content = []
        for t in turns:
            content.append({"type": "text", "text": t["content"]})
        if image_b64:
            content.insert(0, {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": image_b64}})
        body = {"model": self.model, "max_tokens": max_tokens,
                "system": sys_text.strip(), "messages": turns}
        # anthropic wants structured content blocks; rebuild final user turn
        if image_b64:
            body["messages"] = list(turns)
            body["messages"][-1] = {"role": "user", "content": content}
        else:
            body["messages"] = [{"role": t["role"], "content": [
                {"type": "text", "text": t["content"]}]} for t in turns]
        self._last_prompt_text = sys_text + "\n".join(
            str(t.get("content", "")) for t in turns)
        req = urllib.request.Request(
            self.BASE, data=json.dumps(body).encode(),
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                      "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read().decode())

    def _wrap(self, payload, lat):
        txt = "".join(b.get("text", "") for b in payload.get("content", []))
        prompt_text = getattr(self, "_last_prompt_text", "")
        pin, pout, src = _resolve_usage(payload.get("usage"), prompt_text, txt,
                                        in_keys=("input_tokens", "prompt_tokens"),
                                        out_keys=("output_tokens", "completion_tokens"))
        return ModelResponse(text=txt, prompt_tokens=pin, completion_tokens=pout,
                             cost_usd=self._cost(pin, pout), latency_s=lat,
                             token_source=src, raw=payload)


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

    def _post(self, messages, temperature=0.0, max_tokens=256, image_b64=None,
              **kw):
        if not self.url:
            raise SystemExit(
                f"provider {self.name!r} has no endpoint in providers.json")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens}
        if self.effort and self.effort_field:
            body[self.effort_field] = self.effort
        if image_b64:
            content = [{"type": "text", "text": m["content"]} for m in messages]
            content.append({"type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}"}})
            body["messages"] = list(messages)
            body["messages"][-1] = {"role": "user", "content": content}
        self._last_prompt_text = "\n".join(
            str(m.get("content", "")) for m in messages)
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read().decode())

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
        ch = (choices[0].get("message") or {}).get("content") or ""
        if isinstance(ch, list):        # some compat layers return content blocks
            ch = "".join(b.get("text", "") for b in ch if isinstance(b, dict))
        in_keys, out_keys = self._usage_keys()
        prompt_text = getattr(self, "_last_prompt_text", "")
        pin, pout, src = _resolve_usage(payload.get("usage"), prompt_text, ch,
                                        in_keys=in_keys, out_keys=out_keys)
        return ModelResponse(text=ch, prompt_tokens=pin, completion_tokens=pout,
                             cost_usd=self._cost(pin, pout), latency_s=lat,
                             token_source=src, raw=payload)

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
