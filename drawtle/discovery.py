"""Live provider discovery, and the user-side overlay that edits it.

Two things live here, and they are separate on purpose.

**1. `probe(provider)` -- ask the provider, now.**

The rule this module exists to enforce: a number shown to a user must be either
*measured from the provider in this session* or *explicitly labelled as a local
table entry with its provenance*. There is no third category, and in particular
there is no "we cached this last week and will present it as current". The
earlier code had a discovery cache on disk (`live-models.json`-shaped) which
made a stale list indistinguishable from a fresh one; this module has no cache
at all. Every call goes to the network. A caller that wants to avoid repeat
calls must hold the result itself and say how old it is.

What a provider actually publishes on its model-list route is thin: ids, and
for some hosts a context length or modality list. It does not publish pricing.
So each model comes back with per-field provenance rather than one blob:

    {"id": "intern-s1", "id_source": "api",
     "context_window": None, "context_source": None,
     "capabilities": ["image_in", ...], "capability_source": "local_table"}

A `None` context window means the provider did not say and the local table has
no entry either. It never means zero.

**2. `Overlay` -- the user's edits, stored outside the repo.**

`providers.json` and `model_registry.json` are versioned files: they are the
project's shipped defaults and a run's provenance points at them. A dashboard
that wrote to them would mean any user clicking "add provider" produced a dirty
working tree, and `git pull` would conflict with their own edits. So edits made
through the control centre go to a file in the user config directory -- the same
directory `catalog` already uses for credentials -- and are merged *over* the
shipped defaults at read time.

The precedence is deliberate and is the opposite of the credential store's:

  * **Credentials: environment wins over file.** An exported key must always be
    able to override a stale stored one.
  * **Providers: overlay wins over repo.** A user who edits the registry through
    the UI must see their edit take effect, even though the repo file is still
    on disk unchanged.

Both directions are chosen so the *user's explicit action* wins. They are not
inconsistent, they are the same rule applied to different actors.

Nothing here is on the measurement path. It decides which endpoint a call goes
to and what the UI displays; it never touches a score.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PROVIDERS_JSON = os.path.join(HERE, "providers.json")
MODEL_REGISTRY_JSON = os.path.join(HERE, "model_registry.json")

#: Capability vocabulary. Shared with the harness configs this registry was
#: imported from, so a model's capability list means the same thing here as it
#: does there. `image_in` is the load-bearing one for this bench.
CAPABILITIES = (
    "image_in",        # accepts an image in the request. REQUIRED for a frame run.
    "video_in",
    "audio_in",
    "thinking",
    "always_thinking",  # reasons every turn regardless of any control we send
    "tool_use",
)

#: What each capability implies for this bench. Shown in the UI next to a model
#: so the consequence is visible at selection time rather than at run time.
CAPABILITY_NOTES = {
    "image_in": "can read a rendered frame; frame-based mode works",
    "video_in": "not used by this bench, which sends still frames",
    "audio_in": "not used by this bench",
    "thinking": "reasons on some turns; output tokens higher than a plain model",
    "always_thinking": "reasons on EVERY turn; output token counts will be high "
                       "and are a property of the model, not of the bench",
    "tool_use": "not used by this bench -- the model returns JSON, no tools",
}


# --------------------------------------------------------------------- env ---


def load_dotenv(path=None):
    """Load a `.env` file into os.environ, without overwriting what is set.

    Why this exists: the project keeps its keys in `configs/.env`, which is
    gitignored. Reading that file only when a shell happened to source it meant
    a key was present in one terminal and absent in another, and the failure
    looked like "provider rejects key" rather than "key was never loaded".

    An already-set environment variable always wins, so `export X=...` still
    overrides the file -- the same precedence the credential store uses.
    Returns (n_loaded, path_or_None). Never raises: a missing file is normal.
    """
    path = path or os.path.join(ROOT, "configs", ".env")
    if not os.path.exists(path):
        return 0, None
    n = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and not os.environ.get(k):
                    os.environ[k] = v
                    n += 1
    except OSError:
        return 0, None
    return n, path


# ------------------------------------------------------------------ overlay ---

def _config_root():
    """Per-platform config directory. Mirrors catalog._config_root exactly.

    Duplicated rather than imported because `catalog` imports this module, and a
    cycle here would be a real one. Two small platform probes are cheaper than
    an import cycle; if a third appears, this moves to a shared module.
    """
    if os.name == "nt":
        for var in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                return base
        return os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


OVERLAY_DIR = os.path.join(_config_root(), "drawtle-bench")
OVERLAY_FILE = os.path.join(OVERLAY_DIR, "overlay.json")

#: Serialises overlay writes. Re-entrant so `mutate_overlay` can hold it across
#: a read-modify-write while `save_overlay` re-acquires it for the write itself.
_OVERLAY_LOCK = threading.RLock()

#: Shape of an empty overlay. Written on first save so a reader never has to
#: handle "file exists but is missing a key".
_EMPTY = {
    "version": 1,
    "updated": None,
    "providers": {},   # name -> full provider spec, overrides providers.json
    "removed_providers": [],
    "models": {},      # model id -> full model entry, overrides model_registry.json
    "removed_models": [],
}


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def load_overlay():
    """The user's edits. Returns a well-formed dict even if the file is absent."""
    blob = _read_json(OVERLAY_FILE)
    if not isinstance(blob, dict):
        return dict(_EMPTY, providers={}, models={}, removed_providers=[],
                    removed_models=[])
    out = dict(_EMPTY)
    out.update(blob)
    # Guard every collection: a hand-edited file with `"providers": null` would
    # otherwise crash a merge with a TypeError, which reads as a server bug.
    for k in ("providers", "models"):
        if not isinstance(out.get(k), dict):
            out[k] = {}
    for k in ("removed_providers", "removed_models"):
        if not isinstance(out.get(k), list):
            out[k] = []
    return out


class OverlayWriteError(Exception):
    """The overlay could not be written. Carries a message safe to show a user."""


def save_overlay(blob):
    """Write the overlay atomically. Returns the path.

    Raises `OverlayWriteError` with a readable reason rather than letting an
    `OSError` escape: this runs inside an HTTP request, and the container this
    is meant to run in may have a read-only home directory. "Cannot write your
    edits here" is a state the UI can explain; a traceback is not.

    The control centre serves requests on a thread per connection, so two
    requests can reach this at once. Two writers sharing one temp filename
    would interleave into a single corrupt file and then publish it with
    `os.replace` -- so the temp name is made unique per writer, and the whole
    write is serialised under `_OVERLAY_LOCK`.
    """
    blob = dict(blob)
    blob["version"] = 1
    blob["updated"] = time.time()
    blob["updated_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    try:
        with _OVERLAY_LOCK:
            os.makedirs(OVERLAY_DIR, exist_ok=True)
            tmp = f"{OVERLAY_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(blob, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, OVERLAY_FILE)
    except OSError as e:
        raise OverlayWriteError(
            f"could not write {OVERLAY_FILE} ({e.strerror or e.__class__.__name__}). "
            f"Edits are stored outside the repository, so this usually means the "
            f"config directory is read-only -- common inside a container. Set "
            f"XDG_CONFIG_HOME (POSIX) or APPDATA (Windows) to a writable path, "
            f"or edit drawtle/providers.json directly and rebuild.") from e
    return OVERLAY_FILE


def mutate_overlay(fn):
    """Apply `fn(overlay)` to the stored overlay and save it, under one lock.

    A caller that does `ov = load_overlay(); ...; save_overlay(ov)` loses the
    edit if a second request lands between the read and the write. Holding the
    lock across the whole read-modify-write makes the sequence atomic, which is
    what every overlay-editing endpoint actually wants. `fn` may return a value,
    which is passed back to the caller.
    """
    with _OVERLAY_LOCK:
        ov = load_overlay()
        out = fn(ov)
        # save_overlay takes the same re-entrant lock; the nesting is why it is
        # an RLock rather than a Lock.
        save_overlay(ov)
        return out


def merged_providers():
    """Shipped providers with the overlay applied. The registry to actually use."""
    base = (_read_json(PROVIDERS_JSON) or {}).get("providers", {}) or {}
    ov = load_overlay()
    out = {k: dict(v) for k, v in base.items()}
    for name in ov["removed_providers"]:
        out.pop(name, None)
        # A provider the user added and then removed leaves a stale tombstone;
        # harmless, but the edit that re-adds it clears the tombstone, which is
        # why removal is checked before the override is applied.
    for name, spec in ov["providers"].items():
        if name in ov["removed_providers"]:
            continue
        base_spec = dict(out.get(name) or {})
        base_spec.update(spec or {})
        out[name] = base_spec
    return out


def merged_models():
    """Shipped model table with the overlay applied. {id: entry}."""
    base = (_read_json(MODEL_REGISTRY_JSON) or {}).get("models", {}) or {}
    ov = load_overlay()
    out = {k: dict(v) for k, v in base.items()}
    for mid in ov["removed_models"]:
        out.pop(mid, None)
    for mid, entry in ov["models"].items():
        if mid in ov["removed_models"]:
            continue
        merged = dict(out.get(mid) or {})
        merged.update(entry or {})
        out[mid] = merged
    return out


def overlay_status():
    """What the overlay currently holds, for display. No secrets are in it."""
    ov = load_overlay()
    return {
        "path": OVERLAY_FILE,
        "exists": os.path.exists(OVERLAY_FILE),
        "updated_iso": ov.get("updated_iso"),
        "n_providers_added_or_edited": len(ov["providers"]),
        "n_providers_removed": len(ov["removed_providers"]),
        "n_models_added_or_edited": len(ov["models"]),
        "n_models_removed": len(ov["removed_models"]),
        "note": (
            "Edits made here are stored outside the repository, in the user "
            "config directory, and are merged over the shipped defaults at read "
            "time. The versioned files drawtle/providers.json and "
            "drawtle/model_registry.json are never written by the dashboard -- "
            "so a user's edits can never be lost to, or conflict with, a pull."
        ),
    }


# ------------------------------------------------------------------ probing ---


class ProbeError(Exception):
    """A provider could not be asked. Carries a human-readable reason."""


def _extract_rows(payload, spec):
    """Pull model rows out of a discovery payload, keeping what is published.

    Providers disagree about the envelope (`data` vs `models`) and about the id
    field (`id` vs `name`), and Gemini prefixes ids with `models/`. All four
    cases are handled here rather than in the caller, because getting one wrong
    yields a silently short list -- which is indistinguishable from a provider
    that really has few models.
    """
    path = (spec or {}).get("models_path") or "data[].id"
    if path.startswith("models["):
        rows = payload.get("models") or []
    else:
        rows = payload.get("data") or []
    if not isinstance(rows, list):
        return []
    out = []
    for m in rows:
        if isinstance(m, dict):
            out.append(m)
        elif isinstance(m, str):
            out.append({"id": m})
    return out


def _norm_id(raw):
    mid = str(raw or "").strip()
    if mid.startswith("models/"):
        mid = mid[len("models/"):]
    return mid


def _published_metrics(row):
    """What this provider actually said about one model, and nothing else.

    Field names differ per host. Each alias below was seen in a real payload or
    in the two harness configs this registry was imported from; none is
    invented. A field that is absent stays absent -- this function never
    substitutes a default, because a default context window presented as a
    measurement is exactly the defect this module exists to prevent.
    """
    ctx = None
    for k in ("context_length", "context_window", "contextWindow",
              "max_context_size", "max_model_len"):
        v = row.get(k)
        if isinstance(v, int) and v > 0:
            ctx = v
            break

    max_out = None
    for k in ("max_output_length", "max_output", "max_tokens", "maxTokens",
              "max_output_size"):
        v = row.get(k)
        if isinstance(v, int) and v > 0:
            max_out = v
            break

    caps = None
    # Some hosts publish an explicit capability list.
    raw_caps = row.get("capabilities") or row.get("modalities")
    if isinstance(raw_caps, list) and raw_caps:
        caps = sorted({_norm_cap(c) for c in raw_caps if c})
    # Others publish an `input`/`output` modality split, which says the same
    # thing for the one capability this bench cares about.
    inp = row.get("input")
    if caps is None and isinstance(inp, list) and inp:
        caps = sorted({f"{_norm_cap(c)}_in" for c in inp if c})

    price_in = price_out = None
    pricing = row.get("pricing")
    if isinstance(pricing, dict):
        # OpenRouter publishes per-1M-token strings here; convert to the bench's
        # per-1K unit rather than storing a number in the provider's unit and
        # letting a later reader assume otherwise.
        for src, dst in (("prompt", "in"), ("completion", "out"),
                         ("input", "in"), ("output", "out")):
            v = pricing.get(src)
            try:
                if v is not None and float(v) >= 0:
                    if dst == "in":
                        price_in = float(v) / 1000.0
                    else:
                        price_out = float(v) / 1000.0
            except (TypeError, ValueError):
                pass
    return {"context_window": ctx, "max_output": max_out,
            "capabilities": caps, "price_in": price_in, "price_out": price_out}


def _norm_cap(c):
    """Normalise a capability token to this project's vocabulary."""
    c = str(c).strip().lower()
    alias = {
        "image": "image_in", "image_in": "image_in", "vision": "image_in",
        "video": "video_in", "video_in": "video_in",
        "audio": "audio_in", "audio_in": "audio_in",
        "text": "text", "text_in": "text",
        "thinking": "thinking", "reasoning": "thinking",
        "always_thinking": "always_thinking",
        "tool_use": "tool_use", "tools": "tool_use", "function_calling": "tool_use",
    }
    return alias.get(c, c)


def probe(provider, spec=None, timeout=20.0):
    """Ask one provider for its model list, right now. No cache, no fallback.

    Returns a dict:

        ok            bool -- did the call succeed
        provider      the name asked about
        endpoint      the URL actually called (so the answer is auditable)
        models        [{id, context_window, capabilities, ...}] with per-field
                      provenance
        n             len(models)
        error         short reason when ok is False
        key_source    "env:NAME" / "file" / None -- never the key itself
        elapsed_ms    how long the call took
        probed_at     ISO timestamp, so a display can say how old this is

    A failure is a normal return, not an exception, because "the provider did
    not answer" is a state the dashboard has to render.
    """
    from . import catalog as CAT          # local import: catalog imports this

    t0 = time.time()
    spec = spec or merged_providers().get(provider) or {}
    url = spec.get("models_url")
    base = {"ok": False, "provider": provider, "endpoint": url, "models": [],
            "n": 0, "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                               time.localtime(t0)),
            "elapsed_ms": 0}
    if not url:
        base["error"] = ("this provider publishes no model-list endpoint, so "
                         "nothing can be discovered; the local table is the "
                         "only source for its models")
        base["elapsed_ms"] = int((time.time() - t0) * 1000)
        return base

    key, src = CAT.resolve_key(provider)
    base["key_source"] = src
    if spec.get("key_env") and not key and not spec.get("self_hosted"):
        base["error"] = ("no key available (looked in "
                         + ", ".join(spec.get("key_env") or []) + ")")
        base["elapsed_ms"] = int((time.time() - t0) * 1000)
        return base

    try:
        payload = _get(url, key, spec.get("auth") or "bearer", timeout)
    except urllib.error.HTTPError as e:
        base["error"] = {
            401: "key rejected (401)",
            403: "key lacks permission for this route (403)",
            404: "no model-list route at this URL (404)",
            429: "rate limited (429)",
        }.get(e.code, f"HTTP {e.code}")
        base["elapsed_ms"] = int((time.time() - t0) * 1000)
        return base
    except TimeoutError as e:
        base["error"] = f"timed out ({e})"
        base["elapsed_ms"] = int((time.time() - t0) * 1000)
        return base
    except (urllib.error.URLError, OSError) as e:
        base["error"] = f"unreachable ({type(e).__name__})"
        base["elapsed_ms"] = int((time.time() - t0) * 1000)
        return base
    except ValueError as e:
        base["error"] = f"response was not JSON ({e})"
        base["elapsed_ms"] = int((time.time() - t0) * 1000)
        return base

    see_also = {k: dict(v) for k, v in merged_models().items()}
    rows = _extract_rows(payload, spec)
    seen = set()
    models = []
    for row in rows:
        mid = _norm_id(row.get("id") or row.get("name"))
        if not mid or mid in seen:
            continue
        seen.add(mid)
        pub = _published_metrics(row)
        local = see_also.get(mid) or {}
        models.append({
            "id": mid,
            "display_name": row.get("name") or local.get("display_name"),
            # Per-field provenance. `context_window` is whatever the provider
            # said; if it said nothing we fall back to the local table and label
            # which one the number came from, so the UI can show "local table,
            # checked 2026-09-18" rather than an unqualified figure.
            "context_window": (pub["context_window"]
                               if pub["context_window"] is not None
                               else local.get("context_window")),
            "context_source": ("api" if pub["context_window"] is not None
                               else ("local_table" if local.get("context_window")
                                     else None)),
            "max_output": (pub["max_output"] if pub["max_output"] is not None
                           else local.get("max_output")),
            "capabilities": (pub["capabilities"] if pub["capabilities"] is not None
                             else local.get("capabilities")),
            "capability_source": ("api" if pub["capabilities"] is not None
                                  else ("local_table"
                                        if local.get("capabilities") else None)),
            "in_registry": mid in see_also,
            "price_known": bool(local.get("price_known"))
                            or pub["price_in"] is not None,
            "price_in": (pub["price_in"] if pub["price_in"] is not None
                         else local.get("price_in")),
            "price_out": (pub["price_out"] if pub["price_out"] is not None
                          else local.get("price_out")),
        })
    models.sort(key=lambda m: m["id"])
    base.update({"ok": True, "models": models, "n": len(models),
                 "raw_keys": sorted(payload.keys())[:8] if isinstance(payload, dict)
                              else []})
    base["elapsed_ms"] = int((time.time() - t0) * 1000)
    return base


def _get(url, key, auth, timeout):
    """One GET, with the auth style this provider declares.

    `urllib`'s `timeout` is a per-socket-operation timeout, not a wall-clock
    deadline: a connection that stalls mid-TLS-handshake or mid-send never
    trips it, and the call blocks forever. That is not a theoretical concern
    here -- it is what made `/api/probe` hang the control centre when a
    provider stopped answering. So the request runs on a daemon thread and the
    caller enforces the deadline itself.
    """
    if auth == "query" and key:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={urllib.parse.quote(key)}"
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(url, method="GET")
        if key:
            if auth == "x-api-key":
                req.add_header("x-api-key", key)
                req.add_header("anthropic-version", "2023-06-01")
            else:
                req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "drawtle-bench/2.1 (+catalog discovery)")

    box = {}

    def _run():
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                box["ok"] = json.loads(r.read().decode())
        except BaseException as e:                  # noqa: BLE001 - re-raised
            box["err"] = e

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout)
    if "ok" in box:
        return box["ok"]
    if "err" in box:
        raise box["err"]
    raise TimeoutError(
        f"no response within {timeout:.0f}s (connection opened but never "
        f"completed -- the provider is unreachable or not answering)")


def probe_all(providers=None, timeout=12.0):
    """Probe every provider that has a discovery route. Sequential, on purpose.

    Parallel probes would be faster and would also make a rate limit on one
    provider look like a network fault on three, because the error arrives out
    of order. This is a setup screen, not a hot path.
    """
    reg = merged_providers()
    names = sorted(providers if providers is not None else reg.keys())
    out = []
    for name in names:
        spec = reg.get(name) or {}
        if not spec.get("models_url"):
            out.append({"ok": False, "provider": name, "endpoint": None,
                        "models": [], "n": 0, "probed_at": None,
                        "error": "no discovery endpoint", "elapsed_ms": 0,
                        "key_source": None})
            continue
        out.append(probe(name, spec, timeout=timeout))
    return out


# ------------------------------------------------------------------- merge ---


def apply_probe_to_registry(results, only=None, keep_prices=True):
    """Turn probe results into overlay entries, so a discovery can be adopted.

    This is what backs the UI's "adopt discovered models" action. It writes to
    the OVERLAY, never to the versioned registry file.

    Only fields the provider actually published are written. A model whose
    context window the provider did not state is written without one, so it
    stays `None` -- genuinely unknown -- rather than being filled in with a
    plausible number.

    Returns (n_written, overlay_path).
    """
    written = 0

    def _apply(ov):
        nonlocal written
        for res in results:
            if only is not None and res.get("provider") not in only:
                continue
            if not res.get("ok"):
                continue
            prov = res["provider"]
            for m in res.get("models") or []:
                entry = ov["models"].get(m["id"]) or {}
                if m.get("context_source") == "api":
                    entry["context_window"] = m["context_window"]
                if m.get("max_output") is not None:
                    entry["max_output"] = m["max_output"]
                if m.get("capability_source") == "api":
                    entry["capabilities"] = m["capabilities"]
                if not keep_prices and m.get("price_in") is not None:
                    entry["price_in"] = m["price_in"]
                    entry["price_out"] = m["price_out"]
                    entry["price_known"] = True
                entry.setdefault("provider", prov)
                entry["discovered"] = True
                entry["discovered_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime())
                entry["source"] = f"live discovery on {prov}"
                ov["models"][m["id"]] = entry
                written += 1

    path = mutate_overlay(_apply)
    return written, path


def unknown_metric_report():
    """Which models still have an unknown limit, and why it matters.

    A UI that shows a blank cell invites the reading "zero". This produces the
    sentence that belongs next to it.
    """
    models = merged_models()
    gaps = []
    for mid in sorted(models):
        m = models[mid]
        missing = [k for k in ("context_window", "max_output")
                   if m.get(k) is None]
        if not m.get("price_known"):
            missing.append("price")
        if missing:
            gaps.append({"model": mid, "missing": missing})
    return {
        "n_models": len(models),
        "n_with_gaps": len(gaps),
        "gaps": gaps,
        "note": (
            "These fields are unknown, not zero. A blank context window means "
            "the provider published none and no one has looked it up; a blank "
            "price means cost for a run on that model will be reported as "
            "unknown rather than as free."
        ),
    }
