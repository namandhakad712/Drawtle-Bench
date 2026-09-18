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
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field


@dataclass
class ModelResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    raw: dict = field(default_factory=dict)


# Rough token estimate when the API does not report usage. Cheap and good enough
# for cost caps; replace with tiktoken if installed for accuracy.
def _est_tokens(text):
    return max(1, len(text) // 4)


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
                 max_retries=4, **_):
        self.model = model
        self.api_key = api_key
        if self.api_key is None and self.key_env:
            for var in self.key_env:
                self.api_key = os.environ.get(var)
                if self.api_key:
                    break
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.prices = dict(DEFAULT_PRICES)
        if prices:
            self.prices.update(prices)
        # resolve price for this model (exact, else by prefix)
        self.price = self.prices.get(model)
        if self.price is None:
            for k, v in self.prices.items():
                if model.startswith(k):
                    self.price = v
                    break
        if self.price is None:
            self.price = {"in": 0.0, "out": 0.0}

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
        pr = self.price
        return (pin / 1000.0) * pr["in"] + (pout / 1000.0) * pr["out"]


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
        u = payload.get("usage", {}) or {}
        pin = u.get("prompt_tokens") or _est_tokens(ch)
        pout = u.get("completion_tokens") or _est_tokens(ch)
        return ModelResponse(text=ch, prompt_tokens=pin, completion_tokens=pout,
                             cost_usd=self._cost(pin, pout), latency_s=lat)


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
        # OpenAI wants image as a user message; we put text prompt in `messages`
        # and attach the image to the final user turn.
        if image_b64:
            body["messages"] = list(messages)
            body["messages"][-1] = {"role": "user", "content": content}
        req = urllib.request.Request(
            self.BASE, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                      "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read().decode())

    def _wrap(self, payload, lat):
        ch = payload["choices"][0]["message"]["content"]
        u = payload.get("usage", {}) or {}
        pin, pout = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        if not pin:
            pin = _est_tokens(ch)
        if not pout:
            pout = _est_tokens(ch)
        return ModelResponse(text=ch, prompt_tokens=pin, completion_tokens=pout,
                             cost_usd=self._cost(pin, pout), latency_s=lat, raw=payload)


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
        req = urllib.request.Request(
            self.BASE, data=json.dumps(body).encode(),
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                      "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return json.loads(r.read().decode())

    def _wrap(self, payload, lat):
        txt = "".join(b.get("text", "") for b in payload.get("content", []))
        u = payload.get("usage", {}) or {}
        pin, pout = u.get("input_tokens", 0), u.get("output_tokens", 0)
        if not pin:
            pin = _est_tokens(txt)
        if not pout:
            pout = _est_tokens(txt)
        return ModelResponse(text=txt, prompt_tokens=pin, completion_tokens=pout,
                             cost_usd=self._cost(pin, pout), latency_s=lat, raw=payload)


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


_BACKENDS = {"mock": MockBackend, "openai": OpenAIBackend,
             "anthropic": AnthropicBackend, "gemini": GeminiBackend}


def make_backend(kind, model, **kw):
    """Factory. kind in _BACKENDS."""
    if kind not in _BACKENDS:
        raise ValueError(f"unknown backend {kind!r}; have {sorted(_BACKENDS)}")
    return _BACKENDS[kind](model, **kw)


def backend_key_env(kind):
    """Environment variables a backend reads its key from (empty if none).

    Used by the CLI and by `analysis/preflight.py` so that the list of accepted
    variable names lives in exactly one place.
    """
    cls = _BACKENDS.get(kind)
    return tuple(getattr(cls, "key_env", ())) if cls else ()
