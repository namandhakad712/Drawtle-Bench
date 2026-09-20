"""Validation and registry assembly for the control centre.

The HTTP layer accepts edits from a browser, so every value it writes has to be
checked before it becomes configuration. Two rules govern what happens here:

* **Reject, never coerce.** A run id with a slash in it is refused, not
  sanitised into a different run id. A context window of `-1` is refused, not
  clamped to 0. Silent coercion produces configuration that does not match what
  the user typed, and the mismatch is invisible afterwards.
* **A rejected edit is a message, not a stack trace.** Every function returns
  `(ok, value_or_message)` so the caller can put the reason in front of the user.

Nothing here writes to the repository. Edits go to the overlay; see
`drawtle/discovery.py`.
"""
from __future__ import annotations

import os
import re

from drawtle import catalog as CAT
from drawtle import discovery as DSC
from drawtle import models as MOD

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Run ids become filenames, so the character set is deliberately narrow. A
#: slash, a colon or a leading dot in a run id would let a caller write outside
#: the results directory; the check is here rather than at each use site.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Provider names become the value of `--backend` and a key in JSON. Same
#: reasoning, slightly wider set to allow hyphens-through names already shipped.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def safe_run_id(run_id):
    return bool(run_id) and bool(_RUN_ID_RE.match(str(run_id)))


def safe_name(name):
    return bool(name) and bool(_NAME_RE.match(str(name)))


def backend_exists(name):
    """Is this a provider the runner could actually construct?"""
    return str(name) in MOD.providers()


# ------------------------------------------------------------- capabilities ---


def normalise_capabilities(caps):
    """Clean a capability list. Unknown tokens are dropped, not invented.

    Returns (list_or_None, warnings). `None` means unchecked, which is a
    different state from `[]` (declared text-only) and the caller must preserve
    the difference -- the whole frame-capability column depends on it.
    """
    if caps is None:
        return None, []
    if not isinstance(caps, (list, tuple, set)):
        return None, ["capabilities must be a list; recorded as unchecked"]
    out, warn = [], []
    for c in caps:
        c = str(c).strip()
        if c in DSC.CAPABILITIES:
            if c not in out:
                out.append(c)
        elif c:
            warn.append(f"ignored unknown capability {c!r}")
    return sorted(out), warn


# ---------------------------------------------------------------- providers ---


def validate_provider(payload, existing=None):
    """Check one provider edit. Returns (spec, errors).

    `spec` contains only the fields this tool owns; anything else in the payload
    is ignored rather than stored, so a client cannot add arbitrary keys to the
    registry through this route.
    """
    errs = []
    name = str(payload.get("name") or "").strip()
    if not safe_name(name):
        errs.append("name must start with a letter or digit and use letters, "
                    "digits, dot, dash or underscore only")
    url = payload.get("url")
    if url:
        url = str(url).strip()
        if not url.startswith(("http://", "https://")):
            errs.append("the chat endpoint must start with http:// or https://")
    else:
        url = None
    murl = payload.get("models_url")
    if murl:
        murl = str(murl).strip()
        if not murl.startswith(("http://", "https://")):
            errs.append("the model-list endpoint must start with http:// or https://")
    else:
        murl = None
    proto = str(payload.get("protocol") or "openai")
    if proto not in ("openai", "anthropic", "mock"):
        errs.append("protocol must be openai, anthropic or mock")
    auth = str(payload.get("auth") or "bearer")
    if auth not in ("bearer", "x-api-key", "query", "none"):
        errs.append("auth must be bearer, x-api-key, query or none")

    env = payload.get("key_env") or []
    if isinstance(env, str):
        env = [s.strip() for s in env.split(",")]
    env = [str(s).strip() for s in env if str(s).strip()]
    for v in env:
        if not re.match(r"^[A-Z][A-Z0-9_]*$", v):
            errs.append(f"{v!r} is not a valid environment variable name "
                        f"(use capitals, digits and underscores)")

    note = payload.get("context_note")
    note = str(note).strip() if note else None

    if errs:
        return None, errs
    spec = {
        "protocol": proto,
        "url": url,
        "key_env": env,
        "models_url": murl,
        "models_path": payload.get("models_path") or "data[].id",
        "auth": auth,
        "openai_compat": proto == "openai",
        "usage_fields": None,
        "context_note": note,
        "self_hosted": bool(payload.get("self_hosted")),
        "rate_limit": None,
    }
    if existing:
        # Preserve whatever the shipped entry recorded that this form does not
        # edit. An override is a patch, not a replacement: dropping `capabilities`
        # here would silently erase a curated field.
        for k in ("capabilities", "model_provider", "rate_limit", "usage_fields"):
            if k in existing and k not in payload:
                spec[k] = existing[k]
    return spec, []


def save_provider(payload):
    """Write a provider override to the overlay. Returns (name, errors, n_models)."""
    providers = DSC.merged_providers()
    name = str(payload.get("name") or "").strip()
    existing = providers.get(name)
    spec, errs = validate_provider(payload, existing)
    if errs:
        return None, errs, 0
    def _apply(ov):
        ov["providers"][name] = spec
        if name in ov["removed_providers"]:
            ov["removed_providers"].remove(name)  # re-adding clears the tombstone
    DSC.mutate_overlay(_apply)
    # Discovery table is built at import; a new provider must appear in it now.
    CAT.DISCOVERY.update(CAT._load_discovery())
    n = len([m for m in DSC.merged_models().values()
             if m.get("provider") == name])
    return name, [], n


def delete_provider(name):
    """Hide a provider. Returns (ok, message, was_shipped).

    A shipped provider gets a tombstone rather than being erased, so the removal
    is visible in the overlay and can be undone by editing one file. A provider
    that only ever existed in the overlay is dropped outright.

    Idempotent, like `delete_model`: hiding something already hidden is the
    state the caller asked for, not an error.
    """
    providers = DSC.merged_providers()
    overlay = DSC.load_overlay()
    if name not in providers:
        if name in (overlay.get("removed_providers") or []):
            return True, "already hidden", True
        return False, f"no provider called {name!r}", False
    shipped = bool(CAT._read_registry_file().get("providers", {}).get(name))

    def _apply(ov):
        ov["providers"].pop(name, None)
        if shipped and name not in ov["removed_providers"]:
            ov["removed_providers"].append(name)
    DSC.mutate_overlay(_apply)
    CAT.DISCOVERY.pop(name, None)
    return True, ("hidden; the shipped entry is untouched in the repository"
                  if shipped else "removed"), shipped


def reset_provider(name):
    """Drop a provider override, restoring the shipped entry."""
    def _apply(ov):
        had = name in ov["providers"] or name in ov["removed_providers"]
        ov["providers"].pop(name, None)
        if name in ov["removed_providers"]:
            ov["removed_providers"].remove(name)
        return had
    had = DSC.mutate_overlay(_apply)
    CAT.DISCOVERY.update(CAT._load_discovery())
    return had


# ------------------------------------------------------------------- models ---


def validate_model(payload):
    """Check one model edit. Returns (entry, errors).

    A blank numeric field becomes `None` -- UNKNOWN. It never becomes 0. The
    distinction is the project's central data rule: a zero context window reads
    as "cannot be used" and a zero price reads as "free", and both are false
    claims about a model nobody has measured.
    """
    errs = []
    mid = str(payload.get("id") or "").strip()
    # Model ids are opaque provider strings and may contain `/`, `:`, `.` --
    # OpenRouter uses `vendor/model`, token-harbor uses `name:free`. So the check
    # is on length and control characters, not on a restricted alphabet.
    if not mid:
        errs.append("a model id is required")
    elif len(mid) > 160:
        errs.append("model id is implausibly long")
    elif any(ord(c) < 32 for c in mid):
        errs.append("model id contains a control character")

    def _posint(key, label):
        v = payload.get(key)
        if v in (None, ""):
            return None, None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None, f"{label} must be a whole number"
        if n <= 0:
            return None, (f"{label} must be positive; leave it blank to record "
                          f"it as unknown rather than as zero")
        return n, None

    ctx, e = _posint("context_window", "context window")
    errs += [x for x in [e] if x]
    maxout, e = _posint("max_output", "max output")
    errs += [x for x in [e] if x]

    def _posfloat(key, label):
        v = payload.get(key)
        if v in (None, ""):
            return None, None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None, f"{label} must be a number"
        if f < 0:
            return None, f"{label} cannot be negative"
        return f, None

    pin, e = _posfloat("price_in", "input price")
    errs += [x for x in [e] if x]
    pout, e = _posfloat("price_out", "output price")
    errs += [x for x in [e] if x]

    caps, warn = normalise_capabilities(payload.get("capabilities"))

    provider = payload.get("provider")
    provider = str(provider).strip() if provider else None
    if provider and provider not in DSC.merged_providers():
        errs.append(f"no provider called {provider!r} is configured")

    if errs:
        return None, errs
    entry = {
        "provider": provider,
        "context_window": ctx,
        "max_output": maxout,
        "price_in": pin,
        "price_out": pout,
        "price_known": (pin is not None and pout is not None),
        "capabilities": caps,
        "capability_source": payload.get("capability_source") or (
            "manual" if caps is not None else None),
        "notes": (str(payload.get("notes")).strip()
                  if payload.get("notes") else None),
        "source": (str(payload.get("source")).strip()
                   if payload.get("source") else "entered in the control centre"),
    }
    return entry, warn


def save_model(payload):
    """Write a model override to the overlay. Returns (id, errors, warnings)."""
    entry, errs = validate_model(payload)
    if errs:
        return None, errs, []
    mid = str(payload["id"]).strip()

    def _apply(ov):
        merged = dict(ov["models"].get(mid) or {})
        # A patch: keys the caller did not send keep their current merged value.
        for k, v in entry.items():
            if v is not None or k in ("capabilities", "capability_source", "notes",
                                      "price_in", "price_out", "context_window",
                                      "max_output", "price_known"):
                merged[k] = v
        ov["models"][mid] = merged
        if mid in ov["removed_models"]:
            ov["removed_models"].remove(mid)
    DSC.mutate_overlay(_apply)
    return mid, [], []


def delete_model(mid):
    """Hide a model. Returns (ok, message).

    Idempotent on purpose. Deleting a model that is already hidden succeeds: the
    caller asked for a state, and that state holds. Treating it as an error was
    a real defect -- a batch delete that included an already-hidden id, or a
    second click on the same row, produced a 400 and a red toast for an outcome
    that was exactly what the user wanted. The genuinely invalid case is an id
    that has never existed, and that is still refused.
    """
    models = DSC.merged_models()
    overlay = DSC.load_overlay()
    if mid not in models:
        if mid in (overlay.get("removed_models") or []):
            return True, "already hidden"
        return False, f"no model called {mid!r}"
    shipped = mid in (CAT._read_registry_file().get("models") or {})

    def _apply(ov):
        ov["models"].pop(mid, None)
        if shipped and mid not in ov["removed_models"]:
            ov["removed_models"].append(mid)
    DSC.mutate_overlay(_apply)
    return True, ("hidden; the shipped entry is untouched" if shipped
                  else "removed")


# ------------------------------------------------------------- registry view ---


def registry_summary():
    """Counts for the dashboard header. Cheap; no network."""
    providers = DSC.merged_providers()
    models = DSC.merged_models()
    overlay = DSC.load_overlay()
    n_key = 0
    no_key = []
    for name, spec in providers.items():
        if not spec.get("key_env") or spec.get("self_hosted"):
            continue
        key, _src = CAT.resolve_key(name)
        if key:
            n_key += 1
        else:
            no_key.append(name)
    n_frame = sum(1 for m in models.values()
                  if m.get("capabilities") and "image_in" in m["capabilities"])
    return {
        "n_providers": len(providers),
        "n_with_key": n_key,
        "n_discoverable": sum(1 for p in providers.values() if p.get("models_url")),
        "n_models": len(models),
        "n_frame_capable": n_frame,
        "no_key": sorted(no_key),
        "n_overlay_providers": len(overlay["providers"]),
        "n_overlay_models": len(overlay["models"]),
    }


def registry_view():
    """Everything the Providers and Models tabs need, in one response.

    Per-model context windows come from the merged table and are LABELLED as
    such. Nothing here claims to be live: the live numbers only ever come from a
    `probe`, which the user triggers explicitly. A page that mixed cached and
    fresh numbers under one heading would be the exact defect this project has
    been careful to avoid everywhere else.
    """
    providers = DSC.merged_providers()
    shipped_p = CAT._read_registry_file().get("providers", {}) or {}
    models = DSC.merged_models()
    shipped_m = CAT._read_registry_file().get("models", {}) or {}
    overlay = DSC.load_overlay()

    provs = []
    for name in sorted(providers):
        spec = providers[name]
        key, src = CAT.resolve_key(name)
        provs.append({
            "name": name,
            "protocol": spec.get("protocol"),
            "url": spec.get("url"),
            "models_url": spec.get("models_url"),
            "auth": spec.get("auth"),
            "key_env": spec.get("key_env") or [],
            "has_key": bool(key) or not spec.get("key_env")
                        or bool(spec.get("self_hosted")),
            "key_source": src,
            "self_hosted": bool(spec.get("self_hosted")),
            "context_note": spec.get("context_note"),
            "builtin": name in shipped_p,
            "overlay": name in overlay["providers"],
        })

    rows = []
    for mid in sorted(models):
        m = dict(models[mid])
        m["id"] = mid
        m["updated"] = mid in overlay["models"]
        # A capabilities key that is absent means unchecked. Normalise the
        # absence to None so the UI has three states to render rather than two.
        if "capabilities" not in m:
            m["capabilities"] = None
        rows.append(m)

    return {"providers": provs, "models": rows,
            "summary": registry_summary(),
            "overlay_path": DSC.OVERLAY_FILE}


# ---------------------------------------------------------------- preflight ---


def preflight(backend, model):
    """What would happen if this (backend, model) were run. No network call.

    Deliberately does not hit the provider: the question "would this start?"
    should be answerable on a plane, and probing on every keystroke would be
    both slow and rude to the provider. The liveness question is answered by the
    Providers tab's Probe button, which the user asks for explicitly.

    One local check is a real one though: whether a frame rasteriser exists in
    this interpreter. Without it a real-model run cannot produce a single frame,
    and the failure otherwise appears after the run has been started.
    """
    ok = True
    errors, warnings = [], []

    if not backend_exists(backend):
        return {"ok": False, "errors": [f"no provider called {backend!r}"],
                "warnings": [], "key_source": None, "capabilities": None,
                "context_window": None, "price_known": False,
                "price_in": None, "price_out": None}

    spec = MOD.provider_spec(backend) or {}
    key, src = CAT.resolve_key(backend)
    if spec.get("key_env") and not key and not spec.get("self_hosted"):
        ok = False
        errors.append("no key available. Looked for: "
                      + ", ".join(spec.get("key_env") or [])
                      + ". Set one in configs/.env or run "
                        f"`python -m drawtle.catalog set-key {backend}`.")

    # A real backend needs a rasteriser; the mock does not.
    rasteriser = None
    if backend != "mock":
        from drawtle import frames as FR
        rasteriser = FR.rasteriser_status(probe=True)
        if not rasteriser["usable"]:
            ok = False
            errors.append(
                "no usable frame rasteriser in this interpreter, so no maze "
                "image can be produced and a vision run would measure nothing. "
                f"{rasteriser['detail']}")

    info = CAT.model_info(model)
    caps = info.get("capabilities")
    if not info.get("known"):
        warnings.append(f"{model!r} is not in the model table, so its context "
                        f"window and price are unknown. Cost for this run will "
                        f"be reported as unmeasured, not as zero.")
    if caps is None:
        warnings.append("capabilities are unchecked for this model, so whether "
                        "it can read a frame is unknown. The runner will still "
                        "send one.")
    elif "image_in" not in caps:
        warnings.append(
            "this model is recorded WITHOUT image_in -- it cannot read a "
            "rendered frame. A frame-based run against it would measure "
            "nothing, because the model never sees the maze.")
    if info.get("context_window") is None:
        warnings.append("context window unknown; a long episode may fail with "
                        "a context error rather than score low.")
    elif info["context_window"] < 32768:
        warnings.append(f"context window is {info['context_window']:,}, and "
                        f"every prior frame is re-sent each turn, so expect "
                        f"context-limit errors on longer episodes.")

    price_known = bool(info.get("price_known"))
    if not price_known:
        warnings.append("no price on record; any cost figure for this run will "
                        "be reported as unknown rather than as free.")

    n_live = None
    return {"ok": ok, "errors": errors, "warnings": warnings,
            "key_source": src, "capabilities": caps,
            "capability_source": info.get("capability_source"),
            "context_window": info.get("context_window"),
            "price_known": price_known,
            "price_in": info.get("price_in"), "price_out": info.get("price_out"),
            "rasteriser": rasteriser,
            "n_models_available": n_live,
            "provider_note": spec.get("context_note")}
