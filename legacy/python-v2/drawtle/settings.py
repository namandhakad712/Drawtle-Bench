"""User settings for the control centre, stored outside the repository.

The same rule the provider overlay follows, for the same reason: a setting a
user changes in the panel must not dirty the working tree, or `git pull` starts
conflicting with their own preferences. Settings live beside the overlay in the
platform config directory.

Two design rules, both deliberate:

* **Unknown keys are rejected, not stored.** A typo in a settings payload should
  fail loudly. Silently storing `test_mod: true` means the user sets a toggle,
  sees it accepted, and watches it do nothing -- the worst possible outcome for
  a control panel.
* **Every value is validated on the way in AND on the way out.** A settings file
  is editable by hand, so a value read from disk is as untrusted as one from a
  request. A `retention_days` of `"forever"` must not reach a comparison against
  a number.
"""
import json
import os
import threading
import time

from . import discovery as DSC

SETTINGS_FILE = (os.environ.get("DRAWTLE_SETTINGS_FILE")
                 or os.path.join(DSC.OVERLAY_DIR, "settings.json"))

_LOCK = threading.RLock()

#: The complete set of settings, with their defaults. The keys here are the
#: only keys that exist; anything else is refused.
DEFAULTS = {
    # UI
    "theme": "light",                  # light | dark
    "log_follow": True,
    # Data
    "test_mode": False,                # show test runs instead of live ones
    "retention_days": 0,               # 0 = keep everything
    # Capabilities
    "allow_docker_start": False,       # let the panel launch Docker Desktop
    "probe_timeout_s": 20.0,
    # Launch defaults
    "default_backend": "",
    "default_model": "",
    "default_dataset": "",
    # The user's shortlist. Ninety models in one dropdown is a list nobody
    # reads; a starred subset is the one they actually run.
    "favorites": [],
}

THEMES = ("light", "dark")

#: Bounds for the numeric settings. A value outside these is clamped rather than
#: rejected: the intent ("don't keep runs long") is clear, and refusing the whole
#: save over an off-by-one would be pedantic.
_BOUNDS = {
    "retention_days": (0, 3650),
    "probe_timeout_s": (1.0, 300.0),
}


class SettingsError(ValueError):
    """A settings payload that cannot be stored as given."""


class SettingsWriteError(SettingsError):
    """The payload was fine; the config directory is not writable.

    Separate from `SettingsError` so the HTTP layer can answer 409 (the request
    is valid, the environment is not) instead of 400, which would send the user
    looking for a mistake in their own input.
    """


def _coerce(key, value):
    """Validate and normalise one value. Raises SettingsError."""
    default = DEFAULTS[key]
    # Lists first: an empty list is falsy but is not a bool, an int or a str,
    # and the branches below would silently stringify it.
    if isinstance(default, list):
        return _coerce_list(key, value)
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise SettingsError(f"{key} must be true or false")
    if isinstance(default, int):
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{key} must be a whole number")
        lo, hi = _BOUNDS.get(key, (0, 10 ** 9))
        return max(lo, min(hi, n))
    if isinstance(default, float):
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{key} must be a number")
        lo, hi = _BOUNDS.get(key, (0.0, 10 ** 9))
        return max(lo, min(hi, f))
    # str
    s = "" if value is None else str(value)
    if key == "theme" and s not in THEMES:
        raise SettingsError(f"theme must be one of {', '.join(THEMES)}")
    return s


def _coerce_list(key, value):
    """A list-valued setting: deduplicated, trimmed, capped.

    Capped because this list is rendered into a page; an unbounded one is a way
    to make the control centre slow with a settings edit.
    """
    if not isinstance(value, (list, tuple)):
        raise SettingsError(f"{key} must be a list")
    out = []
    for item in value:
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out[:500]


def read():
    """The current settings: defaults, overlaid with whatever is on disk.

    Never raises. A corrupt or unreadable settings file degrades to the defaults
    -- a control panel that refuses to open because its own preferences file is
    malformed is worse than one that opens with default preferences.
    """
    out = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return out
    if not isinstance(blob, dict):
        return out
    for key in DEFAULTS:
        if key not in blob:
            continue
        try:
            out[key] = _coerce(key, blob[key])
        except SettingsError:
            continue                      # keep the default for this one key
    return out


def write(patch):
    """Apply `patch` (a partial dict) and persist. Returns the full settings.

    A patch, not a replacement: two tabs open at once should not clobber each
    other's unrelated settings.
    """
    if not isinstance(patch, dict):
        raise SettingsError("settings payload must be an object")
    unknown = sorted(set(patch) - set(DEFAULTS))
    if unknown:
        raise SettingsError(
            "unknown setting(s): " + ", ".join(unknown)
            + ". Known settings: " + ", ".join(sorted(DEFAULTS)))
    with _LOCK:
        current = read()
        for key, value in patch.items():
            current[key] = _coerce(key, value)
        current["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.localtime())
        try:
            os.makedirs(DSC.OVERLAY_DIR, exist_ok=True)
            tmp = f"{SETTINGS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(current, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, SETTINGS_FILE)
        except OSError as e:
            raise SettingsWriteError(
                f"could not write {SETTINGS_FILE} ({e.strerror or e.__class__.__name__}); "
                f"the config directory may be read-only")
    return current


def describe():
    """Settings plus where they live, for the UI."""
    return {"settings": read(), "path": SETTINGS_FILE,
            "exists": os.path.exists(SETTINGS_FILE),
            "defaults": dict(DEFAULTS)}


def toggle_favorite(mid, on):
    """Add or remove one model id from the shortlist. Returns the new list.

    A dedicated read-modify-write under the lock rather than a client sending
    the whole list back: two tabs toggling different models would otherwise
    each send a full list and one of them would win.
    """
    mid = str(mid or "").strip()
    if not mid:
        raise SettingsError("a model id is required")
    if not isinstance(on, bool):
        on = str(on).lower() in ("true", "1", "yes", "on")
    with _LOCK:
        favs = list(read().get("favorites") or [])
        if on and mid not in favs:
            favs.append(mid)
        elif not on and mid in favs:
            favs.remove(mid)
        return write({"favorites": favs})["favorites"]
