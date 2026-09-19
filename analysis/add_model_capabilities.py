"""Write the capability + provenance metadata into drawtle/model_registry.json.

Run once from the repo root:  python analysis/add_model_capabilities.py

Why a script rather than a hand-edit: the model table is 60 entries, every one
needs a `capabilities` list, and the values come from three different sources
that must be distinguishable in the output --

  * `harness_config` -- the model declared its capabilities in one of the two
    agent-harness configs this registry was imported from. These are the
    provider's or the harness author's own claims, which is the strongest
    evidence available for the models this project has not called yet.
  * `api` -- the provider published a modality list or capability array on its
    model-list endpoint, observed live.
  * `unchecked` -- nobody has said. NOT the same as text-only.

A capability list copied by hand loses that distinction, and the distinction is
the whole point: "this model cannot see" and "we never checked" lead to
different actions.

The script is idempotent -- re-running it rewrites the same values.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REG = os.path.join(ROOT, "drawtle", "model_registry.json")

#: (provider, capabilities, source) keyed by model id.
#:
#: Every entry below is transcribed from a config the user supplied: either
#: `~/.kimi-code/config.toml` ([models."<provider>/<model>"] capability arrays)
#: or `~/.dsh/settings.yaml` (per-model `input: [text, image]`). Where the two
#: disagree, the TOML wins and the disagreement is recorded in `notes` -- a
#: capability list that silently picks one of two conflicting sources is worse
#: than one that reports the conflict.
CAPS = {
    # ---- InternLM / intern (same host, two provider names) ----------------
    "intern-s2": (["image_in", "video_in", "thinking", "tool_use"], "harness_config"),
    "intern-s1-pro": (["image_in", "video_in", "thinking", "tool_use"], "harness_config"),
    "intern-s1": (["image_in", "thinking", "tool_use"], "harness_config"),
    "intern-s1-mini": (["image_in", "video_in", "thinking", "tool_use"], "harness_config"),
    "intern-s2-preview-35b": (["image_in", "video_in", "thinking", "tool_use"], "harness_config"),
    "internvl3.5-241b-a28b": (["image_in", "thinking", "tool_use"], "harness_config"),
    # Discovered live at /v1/models but not in either harness config:
    "intern-s2-preview-397b": (["image_in", "video_in", "thinking", "tool_use"], "harness_config"),

    # ---- Agnes ------------------------------------------------------------
    "agnes-2.0-flash": (["image_in", "thinking", "tool_use"], "harness_config"),
    "agnes-2.5-flash": (["image_in", "thinking", "tool_use"], "harness_config"),
    "agnes-3.0-flash": (["thinking", "tool_use"], "harness_config"),

    # ---- opencode / opencode-custom (same Zen gateway) --------------------
    "big-pickle": (["thinking", "tool_use"], "harness_config"),
    "mimo-v2.5-free": (["image_in", "video_in", "audio_in", "thinking", "tool_use"], "harness_config"),
    "muse-spark-1.2-contributor-free": (["image_in", "video_in", "audio_in", "always_thinking", "tool_use"], "harness_config"),
    "muse-spark-1.2": (["image_in", "video_in", "audio_in", "always_thinking", "tool_use"], "harness_config"),
    "muse-spark-1.3-contributor-free": (["always_thinking", "tool_use"], "harness_config"),
    "nemotron-3-ultra-free": (["thinking", "tool_use"], "harness_config"),
    "nemotron-3.5-lightning-free": (["thinking", "tool_use"], "harness_config"),
    "ling-3.0-flash-fin-free": (["thinking", "tool_use"], "harness_config"),

    # ---- InferX -----------------------------------------------------------
    "Qwen3.6-35B-A3B-FP8": (["image_in", "video_in", "audio_in", "thinking", "tool_use"], "harness_config"),
    "Qwen3-Coder-Next-FP8": (["tool_use"], "harness_config"),
    "Qwen3-Coder-Next-FP8-no-thinking": (["tool_use"], "harness_config"),
    "gemma-4-31B-it-fp8": (["thinking", "tool_use"], "harness_config"),
    "gpt-oss-20b": (["thinking", "tool_use"], "harness_config"),

    # ---- Poolside: both Laguna models are text-only ------------------------
    "poolside/laguna-s-2.1": (["thinking", "tool_use"], "harness_config"),
    "poolside/laguna-xs-2.1": (["thinking", "tool_use"], "harness_config"),

    # ---- Atria ------------------------------------------------------------
    "Atria-Dawn-Preview": (["thinking", "tool_use"], "harness_config"),

    # ---- token-harbor -----------------------------------------------------
    "deepseek-v4.1-flash:free": (["image_in", "thinking", "tool_use"], "harness_config"),
    "deepseek-v4-flash:free": (["thinking", "tool_use"], "harness_config"),
    "mimo-v2.5:free": (["image_in", "video_in", "audio_in", "thinking", "tool_use"], "harness_config"),

    # ---- IFM --------------------------------------------------------------
    "IFM/K2-Horizon-375B-A23B": (["thinking", "tool_use"], "harness_config"),

    # ---- The models already in the table, now with capabilities -----------
    # These four were vetted against the providers' own docs, which list image
    # input for each; the source reflects that rather than a harness config.
    "gemini-2.5-flash": (["image_in", "video_in", "audio_in", "thinking", "tool_use"], "api"),
    "gemini-2.5-flash-lite": (["image_in", "video_in", "audio_in", "thinking", "tool_use"], "api"),
    "gemini-2.5-pro": (["image_in", "video_in", "audio_in", "thinking", "tool_use"], "api"),
    "gemini-3.8-flash": (["image_in", "thinking", "tool_use"], "api"),
    "gpt-4o": (["image_in", "tool_use"], "api"),
    "gpt-4o-mini": (["image_in", "tool_use"], "api"),
    "claude-3-5-sonnet": (["image_in", "tool_use"], "api"),
    "claude-3-5-haiku": (["image_in", "tool_use"], "api"),
    # MockBackend emits a fixed answer and never inspects its input, so it
    # accepts the frame argument the runner builds. It reads text only.
    "mock": ([], "by construction"),
}

#: Model entries to ADD to the table, beyond what is already there. Context
#: windows are the figures published in the harness configs; a model whose
#: window the configs did not state is added with `null`, because an invented
#: window is worse than a missing one.
NEW_MODELS = {
    # nararouter -- a router, so these are upstream models reached through it.
    # Windows from ~/.dsh/settings.yaml, which lists them per id.
    "stepfun-3.7-flash": {
        "provider": "nararouter", "context_window": 262000, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.dsh/settings.yaml (nararouter block, checked 2026-09-19)",
        "checked": "2026-09-19",
        "notes": "Reached through the NaraRouter gateway. The router does not "
                 "publish pricing or an output limit, and neither does the "
                 "upstream vendor's listing that we can see, so both are null.",
    },
    "laguna-s-2.1": {
        "provider": "nararouter", "context_window": 262000, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.dsh/settings.yaml (nararouter block, checked 2026-09-19)",
        "checked": "2026-09-19",
        "notes": "Poolside's Laguna S served through NaraRouter. The id here "
                 "has NO `poolside/` prefix, unlike on the Poolside direct "
                 "endpoint -- two different ids for one model, so a comparison "
                 "across the two providers is comparing transports as well.",
    },
    "atria-dawn": {
        "provider": "nararouter", "context_window": 256000, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.dsh/settings.yaml (nararouter block, checked 2026-09-19)",
        "checked": "2026-09-19",
        "notes": "Atria Dawn through NaraRouter, id `atria-dawn`. The direct "
                 "Atria endpoint uses the capitalised id `Atria-Dawn-Preview`.",
    },
    "tencent-hy3-free": {
        "provider": "nararouter", "context_window": 192000, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.dsh/settings.yaml (nararouter block, checked 2026-09-19)",
        "checked": "2026-09-19",
        "notes": "Hunyuan 3, free tier. The `-free` suffix is part of the id.",
    },
    "ling-3.0-flash-sante-free": {
        "provider": "nararouter", "context_window": 262000, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.dsh/settings.yaml (nararouter block, checked 2026-09-19)",
        "checked": "2026-09-19", "notes": "Router id for the Ling 3.0 Flash "
        "fine-tune. Free tier.",
    },
    "ling-3.0-flash-vl-free": {
        "provider": "nararouter", "context_window": 262000, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.dsh/settings.yaml (nararouter block, checked 2026-09-19)",
        "checked": "2026-09-19",
        "notes": "The VL variant of Ling 3.0 Flash: the `vl` in the id is the "
                 "vision-language marker, so this is the one Ling id worth a "
                 "frame run. Kept separate from the fin and sante variants, "
                 "which are text-only.",
    },
    "nemotron-3-super-free": {
        "provider": "nararouter", "context_window": 262000, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.dsh/settings.yaml (nararouter block, checked 2026-09-19)",
        "checked": "2026-09-19", "notes": "Free tier via NaraRouter.",
    },
    "nex-n2.5-pro": {
        "provider": "nararouter", "context_window": 262000, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.dsh/settings.yaml (nararouter block, checked 2026-09-19)",
        "checked": "2026-09-19", "notes": "Free tier via NaraRouter.",
    },

    # opencode / opencode-custom -- maxTokens figures from the dsh block.
    "muse-spark-1.3-contributor-free": {
        "provider": "opencode", "context_window": 1000000, "max_output": 32000,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.dsh/settings.yaml (opencode-custom block, checked 2026-09-19)",
        "checked": "2026-09-19",
        "notes": "Muse Spark 1.3, free tier. The TOML config lists the 1.2 "
                 "revision with always_thinking; 1.3 was added later and the "
                 "TOML does not cover it, so check the modality before a frame "
                 "run rather than assuming it inherits 1.2's capabilities.",
    },
    "hy3-free": {
        "provider": "opencode", "context_window": 190000, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.kimi-code/config.toml (checked 2026-09-19)",
        "checked": "2026-09-19",
        "notes": "Hunyuan 3 free tier on the Zen gateway. Supports reasoning "
                 "effort low/medium/high per the TOML config.",
        "reasoning_effort_levels": ["low", "medium", "high"],
    },
    "deepseek-v4-flash-vision-exp": {
        "provider": "opencode", "context_window": None, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "live discovery on opencode-custom (2026-09-19)",
        "checked": "2026-09-19",
        "notes": "Discovered live. The `vision-exp` in the id says it takes "
                 "images but the provider publishes no modality list and no "
                 "context window, so both are unchecked here. Experimental, so "
                 "do not build a long comparison on it.",
    },
    "deepseek-v4-flash-free": {
        "provider": "opencode", "context_window": None, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "live discovery on opencode-custom (2026-09-19)",
        "checked": "2026-09-19",
        "notes": "Discovered live; no published window or modality list.",
    },

    # Poolside direct endpoint uses the prefixed id.
    "poolside/laguna-s-2.1": {
        "provider": "poolside", "context_window": 1000000, "max_output": 65536,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.kimi-code/config.toml (checked 2026-09-19); dsh lists "
                  "262144 for the same id",
        "checked": "2026-09-19",
        "notes": "CONFLICT: the TOML config gives max_context_size 1000000 and "
                 "an output limit of 65536, while the dsh config gives 262144 "
                 "and no output limit for the same id. Neither is a measurement. "
                 "The larger figure is recorded here because it comes from the "
                 "richer config, but treat the window as unverified and expect a "
                 "context error past 262K rather than a low score.",
    },
    "poolside/laguna-xs-2.1": {
        "provider": "poolside", "context_window": 256000, "max_output": None,
        "price_in": None, "price_out": None, "price_known": False,
        "source": "~/.kimi-code/config.toml (checked 2026-09-19); dsh lists "
                  "262144 for the same id",
        "checked": "2026-09-19",
        "notes": "Same conflict as laguna-s-2.1, smaller gap (256000 vs "
                 "262144). Unverified.",
    },

    # IFM
    "IFM/K2-Think-v2": {
        "provider": "institute-of-foundation-models", "context_window": None,
        "max_output": None, "price_in": None, "price_out": None,
        "price_known": False,
        "source": "live discovery on institute-of-foundation-models (2026-09-19)",
        "checked": "2026-09-19",
        "notes": "Discovered live; the provider publishes ids only. No key is "
                 "configured for this provider yet, so this entry is a "
                 "placeholder recording that the id exists.",
    },

    # nararouter's very large list. Windows from the dsh file where listed,
    # null otherwise -- the router publishes ids only.
    "claude-fable-5": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "claude-fable-5.1": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "claude-opus-4.7": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "claude-opus-4.8": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "claude-opus-5": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "claude-sonnet-5": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "deepseek-v4-flash-alibaba": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "deepseek-v4-pro-alibaba": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "glm-5.2": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "glm-5.3": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "gpt-5.6-luna": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "gpt-5.6-sol": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "gpt-5.6-terra": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "kimi-k2.7-code": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "kimi-k3": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "mimo-v2.5": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "minimax-m3": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "muse-spark-1.2": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id. On the opencode gateway this model is always_thinking and multimodal; the router likely serves the same weights, but that is an inference and is not recorded as a capability here."},
    "muse-spark-1.2-contributor": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "muse-spark-1.3": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "muse-spark-1.3-contributor": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "gemini-3.1-pro-high": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "gemini-3.8-flash-high": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "agnes-video-v2.0": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Video generation model exposed in the same list as the chat models. It will not answer a chat request -- kept in the table only so a discovery of the router shows it as known rather than new."},
    "agnes-3-flash": {"provider": "nararouter", "context_window": 512000, "source": "~/.dsh/settings.yaml (nararouter block, checked 2026-09-19)", "checked": "2026-09-19", "notes": "Agnes 3 Flash via the router. The dsh config lists image input for this id."},
    "deepseek-v4.1-flash": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "glm-5.3-flash": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},
    "gpt-6-astra": {"provider": "nararouter", "source": "live discovery on nararouter (2026-09-19)", "checked": "2026-09-19", "notes": "Router id; no published window or modalities."},

    # Agnes image/video generation endpoints. Not chat models; recorded so a
    # discovery does not report them as unknown.
    "agnes-image-2.0-flash": {"provider": "agnes", "source": "live discovery on agnes (2026-09-19)", "checked": "2026-09-19", "notes": "IMAGE GENERATION model, not a chat model. It appears in the same discovery list as the chat models because this provider's /v1/models mixes them. A chat request against it will fail."},
    "agnes-image-2.1-flash": {"provider": "agnes", "source": "live discovery on agnes (2026-09-19)", "checked": "2026-09-19", "notes": "IMAGE GENERATION model, not a chat model."},
    "agnes-image-2.5-flash": {"provider": "agnes", "source": "live discovery on agnes (2026-09-19)", "checked": "2026-09-19", "notes": "IMAGE GENERATION model, not a chat model."},
    "agnes-video-2.5": {"provider": "agnes", "source": "live discovery on agnes (2026-09-19)", "checked": "2026-09-19", "notes": "VIDEO GENERATION model, not a chat model."},
    "agnes-video-2.5-flash": {"provider": "agnes", "source": "live discovery on agnes (2026-09-19)", "checked": "2026-09-19", "notes": "VIDEO GENERATION model, not a chat model."},
    "agnes-2.5-pro": {"provider": "agnes", "source": "live discovery on agnes (2026-09-19)", "checked": "2026-09-19", "notes": "Discovered live; no published window or modalities."},
    "agnes-2.5-pro-alpha": {"provider": "agnes", "source": "live discovery on agnes (2026-09-19)", "checked": "2026-09-19", "notes": "Discovered live; no published window or modalities."},
    "agnes-2.5-pro-beta": {"provider": "agnes", "source": "live discovery on agnes (2026-09-19)", "checked": "2026-09-19", "notes": "Discovered live; no published window or modalities."},

    # InferX extras
    "Agents-A1": {"provider": "inferx", "source": "live discovery on inferx (2026-09-19)", "checked": "2026-09-19", "notes": "Discovered live; no published window."},
    "Devstral-2-123B-Instruct-2512-int4-AutoRound": {"provider": "inferx", "source": "live discovery on inferx (2026-09-19)", "checked": "2026-09-19", "notes": "Discovered live; no published window. Quantised, so its output is not comparable to the unquantised release."},
    "Qwen3.8-27B-FP8": {"provider": "inferx", "source": "live discovery on inferx (2026-09-19)", "checked": "2026-09-19", "notes": "Discovered live; no published window."},
    "deepseek-v4-flash-0731": {"provider": "inferx", "source": "live discovery on inferx (2026-09-19)", "checked": "2026-09-19", "notes": "Discovered live; no published window."},
    "deepseek-v4.1-flash": {"provider": "inferx", "source": "live discovery on inferx (2026-09-19)", "checked": "2026-09-19", "notes": "Discovered live; no published window."},
    "glm-5.3-flash": {"provider": "inferx", "source": "live discovery on inferx (2026-09-19)", "checked": "2026-09-19", "notes": "Discovered live; no published window."},
}


def main():
    if not os.path.exists(REG):
        print(f"no registry at {REG}", file=sys.stderr)
        return 1
    with open(REG, "r", encoding="utf-8") as fh:
        reg = json.load(fh)
    models = reg.setdefault("models", {})

    n_caps = n_new = 0
    for mid, (caps, source) in CAPS.items():
        if mid not in models:
            models[mid] = {"provider": None, "source": "capability backfill",
                           "checked": "2026-09-19"}
        models[mid]["capabilities"] = sorted(caps)
        models[mid]["capability_source"] = source
        n_caps += 1

    for mid, entry in NEW_MODELS.items():
        cur = models.get(mid)
        if cur is None:
            models[mid] = dict(entry)
            n_new += 1
        else:
            # Never clobber a field someone already recorded deliberately.
            for k, v in entry.items():
                cur.setdefault(k, v)

    # Capability for the nararouter ids taken from the dsh file, which lists
    # `input: [text, image]` for some of them. Applied after NEW_MODELS so the
    # entry exists.
    for mid in ("agnes-2.5-flash", "agnes-3-flash", "stepfun-3.7-flash"):
        if mid in models and "capabilities" not in models[mid]:
            models[mid]["capabilities"] = ["image_in", "thinking", "tool_use"]
            models[mid]["capability_source"] = "harness_config"

    reg["version"] = reg.get("version", 1) + 1
    reg["updated"] = int(time.time())
    reg["updated_jst"] = time.strftime("%Y-%m-%d", time.localtime())
    reg["_doc"] = (reg.get("_doc") or []) + [
        "",
        "capabilities + capability_source",
        "  Per-model capability list. Vocabulary: image_in, video_in, audio_in,",
        "  thinking, always_thinking, tool_use. `image_in` is the one this bench",
        "  depends on: a model without it cannot read a rendered frame, so a",
        "  frame-based run against it measures nothing.",
        "",
        "  capability_source records WHERE the list came from, and the",
        "  distinction is load-bearing:",
        "    harness_config  declared in an agent-harness config the model was",
        "                    imported from. Not this project's own measurement.",
        "    api             the provider published a modality list, observed",
        "                    live on its model-list endpoint.",
        "    by construction true of the mock backend and nothing else.",
        "  A model with NO `capabilities` key is UNCHECKED. That is not the same",
        "  as text-only, and the UI must not render it as though it were.",
    ]
    with open(REG, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"  capabilities on {n_caps} model(s); {n_new} new model(s); "
          f"{len(models)} total")
    print(f"  wrote {REG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
