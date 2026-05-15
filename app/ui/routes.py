"""HTML routes for the web UI. Server-rendered Jinja2 + HTMX for interactivity.

Only HTML lives here; data routes live in app/api/*. The settings form posts
to /api/settings (PATCH) via HTMX, then re-renders the whole settings panel.
"""
import types
import typing
from typing import Any, get_args, get_origin, get_type_hints

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app import __version__, jobs
from app.api.manage import media_server_client
from app.config import READ_ONLY_FIELDS, SENSITIVE_FIELDS, _EnvSettings, settings
from app.pipeline.lang import LANGUAGE_OPTIONS
from app.server import MediaServerError


# Reusable dropdown options for any field that takes an ISO 639-1 language
# code. Built once from LANGUAGE_OPTIONS so the dropdown surface is the same
# in Settings (default_target_lang) and the Library filter form.
_LANGUAGE_DROPDOWN_OPTIONS = [
    {"value": code, "label": f"{name} ({code})"}
    for code, name in LANGUAGE_OPTIONS
]


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _format_duration(seconds: float | int | None) -> str:
    """Compact human-readable duration: '32s', '5m 32s', '1h 5m'. Used in
    the jobs table next to the progress bar to keep the displayed progress
    honest — if the bar says 50% but the elapsed says 12 minutes on a
    90-second-typical job, something is wrong."""
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


templates.env.filters["duration"] = _format_duration


def _ts_relative(epoch: float | int | None) -> str:
    """Render a Unix epoch time as a compact "N ago" string for the Cache
    Explorer's Modified column. We don't pull `humanize` for one column —
    five buckets cover everything a user actually distinguishes between
    when deciding which cache entry to delete."""
    if not epoch:
        return ""
    import time
    delta = max(0, int(time.time() - epoch))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    if delta < 30 * 86400:
        return f"{delta // 86400}d ago"
    return f"{delta // (30 * 86400)}mo ago"


templates.env.filters["ts_relative"] = _ts_relative

# Expose the server's current wall-clock time to every template render so
# _jobs_table.html can compute elapsed_seconds + snapshot_at directly from
# raw Job timestamps (started_at / finished_at). Without this, the template
# would have to receive pre-augmented dicts — which means routing every
# Job through a serialization step before render. now() is a function so
# each template render gets a fresh value (Jinja calls it per use).
import time as _time
templates.env.globals["now"] = _time.time
# Single source of truth: app/__init__.py:__version__. Exposed as a Jinja
# global so every rendered page can show the running version in its
# footer without each route having to thread it through context.
templates.env.globals["app_version"] = __version__


# ── Settings UI metadata ─────────────────────────────────────────────────────
# Field + section metadata drive the settings form rendering. Sections are
# ordered from "always free / no setup" (top) to "configurable cost" (middle)
# to "tuning knobs most users never touch" (bottom). Inside each dropdown,
# options are also ordered simplest/free → most expensive/complex, and each
# option carries a [BADGE] in its label that calls out the consequence:
#
#   [FREE · LOCAL]            no $, runs offline, no account
#   [FREE TIER · CLOUD]       free up to a quota, paid beyond
#   [VARIES]                  free or paid depending on user's pick
#   [ANY HOST · slow]         no special hardware needed but slow
#   [INTEL iGPU · 5-10× faster]  needs specific hardware, much faster
#   [~500 MB · balanced]      disk footprint + speed/quality trade-off
#
# Whisper choices have NO $ cost — only compute time × disk space.

_SECTION_META: dict[str, str] = {
    "Media server": (
        "START HERE — without this filled in, the Library tab stays empty. "
        "Pick your server type, paste its URL + API key, save."
    ),
    "Defaults": (
        "Pre-set choices applied when you click 'Subtitle this' / 'Subtitle "
        "selected' in the Library without per-job overrides."
    ),
    "Speech-to-Text": (
        "Whisper transcribes audio to text — always free, fully local. "
        "Trade-off is compute time × quality × disk space, not money."
    ),
    "Translation": (
        "Pick the translation provider. NLLB is fully free and local, "
        "DeepL is freemium cloud, LLM uses whatever you wire up below."
    ),
    "Translation model": (
        "LLM that translates subtitle cues — only used when provider = LLM. "
        "Works with cloud (Anthropic / OpenAI / OpenRouter / …) or local "
        "(Ollama / LM Studio / LocalAI / vLLM)."
    ),
    "Subtitles": (
        "WebVTT line-wrap + readability polish. Defaults match pro subtitle norms."
    ),
    "Resource safety": (
        "Caps that prevent a single job from consuming the host. Defaults are "
        "sized for a 2 h film on a 6 GB / 4 vCPU container."
    ),
    "Security": (
        "Optional HTTP Basic auth in front of the whole app. Turn ON on any "
        "network where you wouldn't trust every device to consume your LLM quota."
    ),
}


# Section-level conditional visibility. Same `field/equals` shape as the
# field-level show_if; equals can be a single string or a list (matched as
# "current value is in the list"). The general rule: if a whole section's
# fields aren't going to be used given the current Defaults config, hide
# the section entirely. Simpler interface, less to read for new users.
#
# Note: "Translation" itself is never gated — the provider chooser lives
# there, so hiding the whole section would also hide the only way to
# change provider. Field-level show_if inside the section handles the
# NLLB-only / DeepL-only / LLM-only fields.
_SECTION_SHOW_IF: dict[str, dict] = {
    # The Translation model section is the per-cue-translation LLM config.
    # Only meaningful when provider=llm (NLLB and DeepL ignore it entirely).
    "Translation model": {"field": "default_translation_provider",
                          "equals": "llm"},
}


_FIELD_META: list[dict[str, Any]] = [
    # ── Media server (Emby / Jellyfin / Plex) — REQUIRED FIRST ────────────────
    # Nothing in the rest of the app works until this section is filled in
    # (Library page is empty, "Subtitle this" buttons fail). It is the first
    # thing a fresh user sees — Resource safety / Security live at the bottom
    # so they don't crowd the start of the form.
    {"key": "media_server_type", "section": "Media server",
     "label": "Server type", "type": "select",
     "options": [
         {"value": "emby",  "label": "Emby", "default": True},
         {"value": "jellyfin", "label": "Jellyfin (Emby-compatible API)"},
         {"value": "plex",  "label": "Plex (different API + auth)"},
     ],
     "summary": "Which media server you connect to.",
     "details": "Emby and Jellyfin share an implementation — their REST APIs are "
                "functionally identical (Jellyfin keeps Emby's /Items, /System/Info/Public "
                "endpoints and the X-Emby-Token auth header). Plex uses a different client: "
                "X-Plex-Token auth, /library/sections + /library/metadata/{ratingKey} endpoints."},
    {"key": "media_server_url", "section": "Media server",
     "label": "Server URL", "type": "text",
     "summary": "Where Subtitle This reaches your media server.",
     "details": "Examples:\n"
                "• http://emby:8096 (docker-compose service name)\n"
                "• http://jellyfin:8096\n"
                "• http://plex:32400 (Plex's default port)\n"
                "• http://192.168.1.10:8096 (LAN IP)"},
    {"key": "media_server_api_key", "section": "Media server",
     "label": "API key (Plex: X-Plex-Token)", "type": "password",
     "summary": "Credential used to call your media server's API.",
     "details": "Where to find it:\n"
                "• Emby: admin → Server Settings → Advanced → API Keys\n"
                "• Jellyfin: Dashboard → API Keys\n"
                "• Plex: plex.tv/account → Authorized Devices, or copy the X-Plex-Token "
                "from a logged-in server URL in your browser."},
    {"key": "media_server_verify_ssl", "section": "Media server",
     "label": "Verify SSL certificate (TLS)", "type": "checkbox",
     "summary": "Leave ON unless your server uses a self-signed certificate.",
     "details": "Leave ON when your Server URL is plain http:// (toggle is ignored) OR when "
                "it's https:// with a publicly-trusted certificate (Let's Encrypt behind "
                "Caddy/Nginx, etc.).\n\n"
                "Turn OFF for:\n"
                "• Plex via LAN IP (its bundled cert is for *.plex.direct — hostname won't match)\n"
                "• Emby/Jellyfin behind a self-signed cert\n"
                "• Any homelab reverse-proxy without a CA-issued cert\n\n"
                "Disabling verification means an attacker on your network could MITM the "
                "traffic between this container and the media server — only do it on a "
                "trusted LAN. Advanced alternative: keep this ON and mount a custom CA "
                "bundle into the container, then set SSL_CERT_FILE=/path/to/ca.crt in env."},

    # ── Defaults — workflow knobs ─────────────────────────────────────────────
    # Per-job overrides for target language, mode, and skip behavior. The
    # provider chooser used to live here but moved into the Translation
    # section below — it belongs with the knobs it gates.
    {"key": "default_target_lang", "section": "Defaults",
     "label": "Default target language", "type": "select",
     "options": _LANGUAGE_DROPDOWN_OPTIONS,
     "summary": "Language you want subtitles in (used when no per-job override).",
     "details": "Coverage varies by translation provider:\n"
                "• NLLB and DeepL support ~30 languages well\n"
                "• LLM providers handle whatever the underlying model knows"},
    # NOTE: default_source_lang_priority is intentionally not exposed.
    # Hard-coded default ['en', '*'] in _EnvSettings handles 99% of cases.
    # Power users can override via BABEL_DEFAULT_SOURCE_LANG_PRIORITY.
    # NOTE: default_translation_provider lives in the Translation section
    # below — the provider chooser belongs with the knobs it gates.
    {"key": "default_skip_if_target_audio_exists", "section": "Defaults",
     "label": "Skip when target-language audio is already present", "type": "checkbox",
     "summary": "Saves compute when the file already has an audio track in the target language.",
     "details": "If the file already has audio in the target language, do nothing — the "
                "user can just switch audio track in their player."},
    {"key": "write_detected_language_to_file", "section": "Defaults",
     "label": "Tag detected source language back into the source file (MKV only)", "type": "checkbox",
     "summary": "Writes the detected ISO language into the source file's MKV header (Matroska only — instant, no remux).",
     "details": "When a film's audio track has no language tag (Emby just shows 'Audio'), "
                "language detection runs differently per backend:\n"
                "• OpenVINO needs a Whisper-tiny pre-pass on the first 30 s (can't surface "
                "its own auto-detection through the optimum-intel API)\n"
                "• CPU/faster-whisper detects internally during the main transcribe call\n\n"
                "Either way the transcription gets the right language. With this checkbox "
                "ON, we ALSO write the detected language back into the file's EBML header "
                "via mkvpropedit — instant, modifies only metadata, NEVER touches the "
                "audio/video data sections. Restricted to MKV/MKA/WebM.\n\n"
                "Non-Matroska containers (MP4/MOV/AVI/...) are deliberately left untouched: "
                "an ffmpeg remux would technically preserve audio byte-for-byte but "
                "rewrites the whole file with documented edge cases (timestamp "
                "re-derivation, lost custom metadata) — not worth the risk on a media "
                "library. Turn this off entirely to keep all source files pristine."},

    # ── Vocal isolation (Demucs) ──────────────────────────────────────────────
    # Single user-facing knob. The model identifier (htdemucs vs
    # htdemucs_ft vs mdx_extra_q) and other complexity is hidden —
    # power users override via the BABEL_VOCAL_ISOLATION_MODEL env var.
    {"key": "vocal_isolation_mode", "section": "Speech-to-Text",
     "label": "Vocal isolation (Demucs)",
     "type": "select",
     "options": [
         {"value": "off",     "label": "OFF — skip the phase", "default": True},
         {"value": "chunked", "label": "CHUNKED — safe RAM, recommended when ON"},
         {"value": "full",    "label": "FULL — needs ≥ 12 GB free RAM"},
     ],
     "summary": "OFF by default. Turn ON for action/sci-fi films where score buries dialogue (Inception, Dunkirk).",
     "details": "Splits the source audio into stems and feeds only the VOCALS stem to "
                "Whisper. Score, ambience, and SFX are removed so Whisper transcribes "
                "a clean speech signal — closes most of the 'climax dialog buried under "
                "music' gap on score-heavy films.\n\n"
                "• OFF (default) — no isolation. Best for dialog-driven dramas with "
                "sparse music where the gain wouldn't justify the cost. Also automatic "
                "for 5.1+ sources (center-channel extraction does the job for free).\n"
                "• CHUNKED — recommended when ON. Processes audio in 5-min chunks, "
                "capping peak RAM at ~1 GB regardless of film length. Sub-second seam "
                "artifacts are invisible to Whisper (it resyncs every 30 s window).\n"
                "• FULL — one apply_model pass over the whole audio. Slightly cleaner "
                "separation (no seam artifacts), but peak RAM scales with film length "
                "× num_stems. A 2.5 h 4-stem run needs ~16 GB peak — only pick this "
                "if your host has 32+ GB free."},
    {"key": "vocal_isolation_chunk_seconds", "section": "Speech-to-Text",
     "label": "Chunk size (seconds)", "type": "number",
     "show_if": {"field": "vocal_isolation_mode", "equals": "chunked"},
     "summary": "How many seconds of audio per Demucs pass. Default 300 (5 min) caps peak RAM at ~1 GB.",
     "details": "apply_model peak memory scales linearly with this value.\n\n"
                "• 300 (default) — safe under a 6 GB cgroup\n"
                "• 120 (2 min) — drop here if you still see 'process restarted' during "
                "isolating-vocals\n"
                "• < 60 — starts hurting separation quality near chunk boundaries"},

    # ── Speech-to-Text ────────────────────────────────────────────────────────
    {"key": "whisper_backend", "section": "Speech-to-Text",
     "label": "Backend", "type": "select",
     "options": [
         {"value": "cpu",      "label": "cpu — runs on any host", "default": True},
         {"value": "openvino", "label": "openvino — Intel iGPU, 5–10× faster"},
     ],
     "summary": "Which Whisper runtime. cpu is universal; openvino needs an Intel iGPU + the openvino image.",
     "details": "• cpu uses faster-whisper, runs entirely on the CPU. INT8 quantization "
                "keeps it tractable but slow.\n"
                "• openvino exports Whisper to OpenVINO IR and runs inference on the "
                "Intel iGPU. Only works in the openvino-flavored image with /dev/dri "
                "exposed (TrueNAS Scale with an N305/N100/iGPU-equipped Intel host).\n\n"
                "Note: even with openvino, several pipeline steps stay CPU-bound — "
                "ffmpeg audio extraction, the language-detection pre-pass, and the "
                "first run's IR conversion (5–30 min, one-off). 100 % CPU during those "
                "phases is normal.\n\n"
                "The cpu backend ALSO unlocks per-word timestamps + the confidence-"
                "gated re-transcription pass (see the auto-improvements banner above)."},
    {"key": "whisper_model", "section": "Speech-to-Text", "label": "Whisper model", "type": "select",
     "options": [
         {"value": "tiny",           "label": "tiny — ~75 MB · smoke tests"},
         {"value": "base",           "label": "base — ~150 MB · fast, ok quality"},
         {"value": "small",          "label": "small — ~500 MB · balanced", "default": True},
         {"value": "medium",         "label": "medium — ~1.5 GB · great quality"},
         {"value": "large-v3",       "label": "large-v3 — ~3 GB · best quality, slowest"},
         {"value": "large-v3-turbo", "label": "large-v3-turbo — ~1.5 GB · near-best, ~2× faster than large-v3"},
     ],
     "summary": "Bigger = better but slower and more RAM. All sizes are free and local.",
     "details": "Whisper has no API cost — all model sizes run locally. First use of a "
                "model triggers a one-off download to /cache.\n\n"
                "Quality jumps:\n"
                "• tiny → base: notable\n"
                "• base → small: notable (the most common sweet-spot)\n"
                "• small → medium: moderate\n"
                "• medium → large-v3: small but real\n"
                "Pick the largest that fits in your RAM budget."},
    {"key": "whisper_compute_type", "section": "Speech-to-Text",
     "label": "Compute type", "type": "select",
     "show_if": {"field": "whisper_backend", "equals": "cpu"},
     "options": [
         {"value": "int8",    "label": "int8 — ~500 MB / ~1.5 GB resident", "default": True},
         {"value": "int16",   "label": "int16 — ~1 GB / ~3 GB · marginal gain"},
         {"value": "float16", "label": "float16 — ~1 GB / ~3 GB · slower than int16"},
         {"value": "float32", "label": "float32 — QUALITY MODE · 16+ GB RAM only"},
     ],
     "summary": "int8 (default) is right for 99 % of users — best balance of RAM, speed, and quality.",
     "details": "Quantization for faster-whisper. Resident-RAM figures below are for the "
                "small / medium model:\n\n"
                "• int8 (default) — balances RAM, speed, quality. Subtitle-level WER vs. "
                "float32 is below the noise floor on most films.\n"
                "• int16 — marginal precision gain over int8; rarely worth the doubled RAM.\n"
                "• float16 — same RAM as int16 but slower on CPU. Mostly for CUDA paths.\n"
                "• float32 — full-precision weights, ~4× int8 RAM (~2 GB small, ~6 GB "
                "medium, ~12 GB large-v3). Only pick this on a fat host (16+ GB free); "
                "TrueNAS's typical 6 GB cgroup will OOM-kill the container at model-load. "
                "WER improvement over int8 is real but small (~5–10 % relative) — not "
                "worth the RAM cost unless you're optimising for festival-grade output."},
    {"key": "whisper_device", "section": "Speech-to-Text",
     "label": "Device", "type": "select",
     "show_if": {"field": "whisper_backend", "equals": "cpu"},
     "options": [
         {"value": "cpu",  "label": "cpu — works on any host", "default": True},
         {"value": "cuda", "label": "cuda — needs an NVIDIA GPU + nvidia-container-toolkit"},
     ],
     "summary": "Where faster-whisper runs. cuda only matters if you've added an NVIDIA GPU."},
    # openvino_device removed from the UI: AUTO is always the right
    # answer (it picks GPU when available, falls back to CPU silently).
    # Explicit GPU/CPU forcing confused users more than it helped.
    # Power users can still override via BABEL_OPENVINO_DEVICE env var.
    {"key": "stt_region_packing", "section": "Speech-to-Text",
     "label": "Region packing (OpenVINO) — fast mode", "type": "checkbox",
     "show_if": {"field": "whisper_backend", "equals": "openvino"},
     "summary": "ON groups many short speech bits into one Whisper pass — 10× faster, near-identical accuracy.",
     "details": "ON (default, fast): groups many short speech bits into a single Whisper "
                "transcription pass. A 2 h film takes ~10 minutes on an Intel iGPU. The "
                "trade-off is that Whisper can occasionally lose track of timing when too "
                "many bits are bundled, which used to lose dialog. Mitigated since 0.7.11 "
                "by the density cap (see field below) and snap recovery — in practice ON "
                "now produces near-OFF accuracy on most films.\n\n"
                "OFF (slow, max accuracy): each speech segment gets its own Whisper pass. "
                "The same 2 h film now takes ~1.5–2 h of transcription on the same iGPU — "
                "roughly 10× slower. Turn OFF only when ON is still missing visible dialog "
                "after tuning the density cap.\n\n"
                "Ignored on the cpu/faster-whisper backend (it has its own longform batching)."},
    {"key": "stt_max_regions_per_window", "section": "Speech-to-Text",
     "label": "Region packing — max regions per pass", "type": "number",
     "show_if": {"field": "whisper_backend", "equals": "openvino"},
     "summary": "Density cap. Default 4 is safe; most users should leave this alone.",
     "details": "Hard cap on how many short speech bits get bundled into one Whisper pass. "
                "Lower = more accurate timing (less risk of losing dialog), slower "
                "transcription. Higher = faster, more risk.\n\n"
                "Reference points:\n"
                "• 4 (default) — recommended\n"
                "• 8 — faster, still safe with snap recovery\n"
                "• 0 — no cap (legacy pre-0.7.11 behavior — drops ~40 % of dialog on dense films)"},

    # ── Translation (provider chooser + provider-specific params) ─────────────
    # The provider chooser lives at the top of this section so it gates the
    # NLLB/DeepL/LLM-specific knobs that follow. The translation_batch_size
    # field (LLM-only) lives in the Translation model section instead — it's
    # part of the LLM config block, not the provider block.
    {"key": "default_translation_provider", "section": "Translation",
     "label": "Translation provider — pick your cost tier", "type": "select",
     "options": [
         {"value": "nllb",  "label": "NLLB — FREE local · ~30 langs well · ~1.5 GB download", "default": True},
         {"value": "deepl", "label": "DeepL — free 500k chars/mo, then paid · best on EU/Asian pairs"},
         {"value": "llm",   "label": "LLM — best quality · free if local (Ollama) or paid (Claude/GPT/…)"},
     ],
     "summary": "Cheapest first. NLLB needs no account; DeepL has a free tier; LLM is the highest quality.",
     "details": "• NLLB — fully free, local, no account, no key. Decent quality on ~30 "
                "well-supported language pairs. Works on both image flavors (uses Intel "
                "iGPU via OpenVINO when available, falls back to CPU torch otherwise — "
                "slower but no setup either way).\n"
                "• DeepL — free 500k characters/month (~6 movies), then paid. Excellent "
                "quality on European and East-Asian pairs. The DeepL API key field "
                "appears below when you pick this.\n"
                "• LLM — uses whatever you configure in the Translation model section. "
                "Highest quality, supports any language pair. Free if you point at local "
                "Ollama / LM Studio. Paid per-token if you point at Anthropic / OpenAI / "
                "OpenRouter / etc."},
    {"key": "nllb_model", "section": "Translation",
     "label": "NLLB model variant", "type": "select",
     "show_if": {"field": "default_translation_provider", "equals": "nllb"},
     "options": [
         {"value": "facebook/nllb-200-distilled-600M",
          "label": "distilled-600M — ~1.5 GB · balanced", "default": True},
         {"value": "facebook/nllb-200-distilled-1.3B",
          "label": "distilled-1.3B — ~3 GB · better fluency, 2× slower"},
         {"value": "facebook/nllb-200-1.3B",
          "label": "1.3B (non-distilled) — ~5 GB · slightly different quality profile"},
         {"value": "facebook/nllb-200-3.3B",
          "label": "3.3B — ~7 GB · best · very slow on CPU, needs ~16 GB RAM"},
     ],
     "summary": "Meta NLLB-200 size. Default 600M is the sweet spot; pick 1.3B for noticeably better fluency at 2× cost.",
     "details": "Bigger = better translations but slower and more RAM. First use of a "
                "variant downloads the weights (one-off, cached in /cache/nllb-models)."},
    {"key": "nllb_batch_size", "section": "Translation",
     "label": "Cues per NLLB batch", "type": "number",
     "show_if": {"field": "default_translation_provider", "equals": "nllb"},
     "summary": "Default 4 is conservative for NLLB-1.3B in 12 GB. Bump to 8–16 on the 600M variant.",
     "details": "Higher = fewer model.generate() calls; lower = less peak RAM per call (the "
                "KV cache scales with batch × seq_len). Default 4 is tuned for NLLB-1.3B + "
                "a 12 GB cgroup with Whisper-large page cache lingering."},
    {"key": "nllb_load_in_8bit", "section": "Translation",
     "label": "Compress NLLB weights to int8 (OpenVINO path)", "type": "checkbox",
     "show_if": {"field": "default_translation_provider", "equals": "nllb"},
     "summary": "Halves NLLB resident RAM. ON by default — quality drop is below the noise floor.",
     "details": "Quantizes weights to int8 via NNCF at load time (~3 GB → ~1.5 GB for "
                "distilled-1.3B). First-time load pays a 1–2 min quantization cost; the "
                "result is cached on disk. Quality cost is roughly 0.3 BLEU — below the "
                "noise floor for subtitle work.\n\n"
                "Default ON because the 1.3B variant otherwise doesn't fit in 12 GB of "
                "cgroup alongside Whisper's page cache. Turn OFF only if you have 16+ GB "
                "headroom and want strict full-precision weights. No effect on the CPU/"
                "torch fallback path."},
    {"key": "deepl_batch_size", "section": "Translation",
     "label": "Cues per DeepL request", "type": "number",
     "show_if": {"field": "default_translation_provider", "equals": "deepl"},
     "summary": "Default 50 is DeepL's API maximum. Lower it for more granular retries at the cost of more round-trips."},
    {"key": "deepl_api_key", "section": "Translation",
     "label": "DeepL API key", "type": "password",
     "show_if": {"field": "default_translation_provider", "equals": "deepl"},
     "summary": "Required when provider = DeepL. Get one at deepl.com/pro-api (Free plan = 500k chars/month).",
     "details": "Free-tier keys end in ':fx' (auto-detected — Babel routes to "
                "api-free.deepl.com vs api.deepl.com)."},

    # ── Translation model (only used when provider=llm) ───────────────────────
    {"key": "translation_batch_size", "section": "Translation model",
     "label": "Cues per LLM batch", "type": "number",
     "summary": "Default 30. Higher = fewer round-trips; lower = more granular retries on failure.",
     "details": "Only affects the LLM provider — NLLB and DeepL have their own batch sizes "
                "in the Translation section above."},
    {"key": "translation_llm_type", "section": "Translation model",
     "label": "Wire protocol", "type": "select",
     "options": [
         {"value": "openai_compat", "label": "openai_compat — universal (OpenAI, Ollama, LM Studio, OpenRouter, …)"},
         {"value": "anthropic",     "label": "anthropic — Claude only (adds prompt caching + adaptive thinking)", "default": True},
     ],
     "summary": "Pick anthropic ONLY when the Model field is a Claude variant; otherwise pick openai_compat.",
     "details": "• openai_compat — universal Chat-Completions protocol; covers all local "
                "servers and most cloud providers (OpenAI, OpenRouter, Together, Groq, "
                "DeepSeek, Zhipu, Gemini-compat, vLLM, llama.cpp, Ollama, LM Studio, "
                "LocalAI).\n"
                "• anthropic — Claude only. You get prompt caching, adaptive thinking, "
                "and strict JSON-schema enforcement that the openai_compat path can't "
                "express."},
    {"key": "translation_llm_model", "section": "Translation model",
     "label": "Model", "type": "text",
     "summary": "Model identifier. Pick a large multilingual, instruction-following model.",
     "details": "Suggestions by tier:\n"
                "• Frontier cloud (paid): claude-opus-4-7, gpt-4o, gemini-1.5-pro, mistral-large\n"
                "• Frontier open-source (free if local): qwen2.5:72b, deepseek-v3, "
                "llama3.1:70b, glm-4-flash, command-r-plus\n"
                "• Cheap & fast: claude-haiku-4-5, gpt-4o-mini, qwen2.5:14b, llama3.1:8b, gemma2:9b"},
    {"key": "translation_llm_endpoint", "section": "Translation model",
     "label": "Endpoint URL (only when wire protocol = openai_compat)", "type": "text",
     "summary": "Where the OpenAI-compatible API lives. Ignored when wire protocol = anthropic.",
     "details": "Cloud endpoints:\n"
                "• https://api.openai.com/v1\n"
                "• https://openrouter.ai/api/v1\n"
                "• https://api.deepseek.com/v1\n"
                "• https://open.bigmodel.cn/api/paas/v4 (Zhipu)\n"
                "• https://generativelanguage.googleapis.com/v1beta/openai (Gemini-compat)\n\n"
                "Local (no API key needed):\n"
                "• http://ollama:11434/v1\n"
                "• http://lmstudio:1234/v1\n"
                "• http://localai:8080/v1\n"
                "• http://host.docker.internal:1234/v1 (LM Studio on the host machine when Babel runs in Docker)"},
    {"key": "translation_llm_api_key", "section": "Translation model",
     "label": "API key (LEAVE BLANK for local servers)", "type": "password",
     "summary": "Required for cloud providers; LEAVE BLANK for local servers (Ollama / LM Studio / LocalAI).",
     "details": "REQUIRED for cloud providers (Anthropic, OpenAI, OpenRouter, Together, "
                "Groq, DeepSeek, Zhipu, Gemini, …).\n\n"
                "LEAVE BLANK for local servers that don't authenticate by default — Babel "
                "substitutes a placeholder so the OpenAI SDK is happy. Set a value only "
                "if you've explicitly enabled auth on your local server (e.g. vLLM with "
                "--api-key)."},
    # ── Subtitle formatting ───────────────────────────────────────────────────
    {"key": "max_line_chars", "section": "Subtitles",
     "label": "Max chars per line", "type": "number",
     "summary": "Default 42. Standard subtitling guidelines: 40–42 for comfortable reading."},
    {"key": "max_lines_per_cue", "section": "Subtitles",
     "label": "Max lines per cue", "type": "number",
     "summary": "Default 2. Overflow merges into the last line — never drops content."},
    {"key": "polish_enabled", "section": "Subtitles",
     "label": "Readability polish (extend short cues, merge fragments)",
     "type": "checkbox",
     "summary": "ON by default. Extends tight Whisper timings to readable durations and merges fragments.",
     "details": "Whisper outputs tight per-utterance timing — a 0.3 s 'Yes.' gets a 0.3 s "
                "cue, far too brief to read. With this ON (default), a final pass extends "
                "cues to a minimum display duration (capped to never overlap the next cue) "
                "and optionally merges adjacent fragments that visually read as one subtitle.\n\n"
                "Inception comparison: raw Whisper had 42.8 % of cues under 1 s, the pro "
                "reference SRT had 0. Defaults match the pro shape closely.\n\n"
                "Turn OFF only if you want the raw Whisper timing (e.g. for downstream "
                "tooling that does its own readability pass)."},
    {"key": "min_cue_duration_seconds", "section": "Subtitles",
     "label": "Min display duration (s)", "type": "number",
     "show_if": {"field": "polish_enabled", "equals": "true"},
     "summary": "Default 1.2 s. Lower bound on how long any cue stays on screen.",
     "details": "1.2 s sits at the lower end of pro subtitling norms (BBC: 0.7–1.5 s "
                "minimum; Netflix: ~0.83 s minimum for single-syllable utterances). "
                "Shorter cues are extended forward (never the start — audio onset stays "
                "in sync)."},
    {"key": "min_seconds_per_char", "section": "Subtitles",
     "label": "Min reading speed (seconds per character)",
     "type": "number",
     "show_if": {"field": "polish_enabled", "equals": "true"},
     "summary": "Default 0.045 s/char ≈ 22 chars/second — a relaxed reading pace.",
     "details": "A 40-character two-line cue claims at least 40 × 0.045 = 1.8 s. Tighten "
                "(smaller value) for faster readers; loosen for slower."},
    {"key": "merge_adjacent_cues", "section": "Subtitles",
     "label": "Merge adjacent short cues", "type": "checkbox",
     "show_if": {"field": "polish_enabled", "equals": "true"},
     "summary": "ON by default. Collapses two consecutive cues into one when they're visually a single subtitle.",
     "details": "Conditions: small gap between them AND combined text fits in the line-wrap "
                "budget AND combined duration stays under the merged-duration cap. Trims "
                "the 'flickery sequence of short fragments' effect Whisper produces on "
                "quick back-and-forth dialog."},
    {"key": "max_gap_to_merge_seconds", "section": "Subtitles",
     "label": "Max gap to merge (s)", "type": "number",
     "show_if": {"field": "merge_adjacent_cues", "equals": "true"},
     "summary": "Default 0.3 s — about a natural breath in conversation. Above this, cues stay separate."},
    {"key": "max_merged_cue_duration_seconds", "section": "Subtitles",
     "label": "Max merged cue duration (s)", "type": "number",
     "show_if": {"field": "merge_adjacent_cues", "equals": "true"},
     "summary": "Default 7 s — the standard upper bound. Past this a subtitle reads as cluttered."},
    {"key": "cue_separation_seconds", "section": "Subtitles",
     "label": "Min gap between cues (s)", "type": "number",
     "show_if": {"field": "polish_enabled", "equals": "true"},
     "summary": "Default 0.125 s ≈ 3 frames at 24 fps — the BBC / Netflix professional norm.",
     "details": "Minimum silence kept between consecutive cues after the polish pass "
                "extends durations. Prevents subtitles flashing one frame off (the "
                "pre-0.7.33 default of 0.05 s technically worked but rendered as rushed "
                "in side-by-side comparison with pro work)."},

    # ── Resource safety (advanced — sits at the bottom of the form) ─────────
    # These caps prevent a long film + heavy mode from consuming all host
    # RAM. They complement the cgroup limits in docker-compose.yml.
    {"key": "job_timeout_seconds", "section": "Resource safety",
     "label": "Job wall-clock timeout (seconds)", "type": "number",
     "summary": "Default 5400 (90 min) — generous for a 3 h film at whisper-large/int8. Set to 0 to disable.",
     "details": "Enforced at every pipeline checkpoint (between audio segments, between "
                "translation batches, etc.) so a wedged job can't hold the queue "
                "indefinitely."},
    {"key": "stt_audio_segment_seconds", "section": "Resource safety",
     "label": "OpenVINO STT audio-segment size (seconds)", "type": "number",
     "summary": "Default 600 (10 min). Caps OpenVINO audio RAM at ~75 MB regardless of film length.",
     "details": "Lower values reduce RAM further; higher values produce fewer segment-"
                "boundary cue splits. Ignored when whisper_backend = cpu "
                "(faster-whisper streams from disk on its own)."},
    # ── Security ────────────────────────────────────────────────────────────
    {"key": "auth_credentials", "section": "Security",
     "label": "HTTP Basic credentials (user:password)", "type": "password",
     "summary": "Blank = no auth (default). Set 'user:password' to require Basic auth on every endpoint except /health.",
     "details": "Adds a same-origin check on POST/PATCH/PUT/DELETE so a malicious LAN "
                "page can't ride your saved browser credentials to start jobs. Apply on "
                "any network where you wouldn't trust every device — the Library page "
                "can queue jobs that consume your LLM quota."},
]


def _section_groups() -> list[tuple[str, str, dict | None, list[dict]]]:
    """Group fields by section. Returns (name, description, show_if_or_None,
    fields) tuples in display order. `show_if`, when present, is a normalized
    dict {field, equals_csv} ready for template data attributes."""
    seen: list[str] = []
    for f in _FIELD_META:
        if f["section"] not in seen:
            seen.append(f["section"])
    out: list[tuple[str, str, dict | None, list[dict]]] = []
    for s in seen:
        rule = _SECTION_SHOW_IF.get(s)
        normalized = _normalize_show_if(rule) if rule else None
        out.append((
            s,
            _SECTION_META.get(s, ""),
            normalized,
            [f for f in _FIELD_META if f["section"] == s],
        ))
    return out


def _normalize_show_if(rule: dict) -> dict:
    """Normalize a show_if rule to {field, equals_csv} so template + JS see
    a uniform shape. `equals` accepts either a string (single value) or a
    list (any-of). Returns equals_csv as a comma-joined string."""
    eq = rule.get("equals")
    values = eq if isinstance(eq, list) else [eq]
    return {"field": rule["field"], "equals_csv": ",".join(str(v) for v in values)}


# Normalize every field-level show_if at module load so the template doesn't
# need to handle string-vs-list. Sections are normalized inline in
# _section_groups since their rules live in _SECTION_SHOW_IF.
for _f in _FIELD_META:
    if "show_if" in _f and "equals_csv" not in _f["show_if"]:
        _f["show_if"] = _normalize_show_if(_f["show_if"])


def _unwrap_optional(target: Any) -> Any:
    """Strip Optional[X] / X | None down to X. Used by _coerce so an
    `int | None` field still coerces to int. Returns the bare type when
    the annotation is `X | None`, otherwise returns it unchanged."""
    origin = get_origin(target)
    # Both typing.Union[X, None] and the PEP 604 `X | None` form report as
    # Union under get_origin — the second one returns types.UnionType in
    # Python 3.10+, the first returns typing.Union. Either way, get_args
    # gives us the members.
    if origin is typing.Union or origin is types.UnionType:
        non_none = [a for a in get_args(target) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return target


def _coerce(key: str, raw: str) -> Any:
    """Coerce a form-submitted string to the type pydantic expects on the env model.

    Uses `typing.get_origin` / `get_args` to inspect the annotation rather
    than substring-matching against `str(target)`. The previous
    `"bool" in str(target)` approach worked for the current field set but
    would silently mis-dispatch any future annotation that mentions "bool"
    in a non-bool position (e.g. a `Literal["bool"]` field). The principled
    inspection drops that footgun.

    Empty number fields coerce to 0 / 0.0 rather than raising — matches the
    UX expectation that clearing a number input means "use the default-ish
    value" (pydantic's Field bounds catch the edge cases where 0 is out
    of range).
    """
    hints = get_type_hints(_EnvSettings)
    target = _unwrap_optional(hints.get(key, str))
    origin = get_origin(target)

    # `bool` check has to come before `int` because `isinstance(True, int)`
    # is True and `bool` is a subclass of `int` in Python.
    if target is bool:
        return raw in ("on", "true", "True", "1", "yes")
    if target is int:
        return int(raw) if raw != "" else 0
    if target is float:
        return float(raw) if raw != "" else 0.0
    # `list[str]` / `tuple[str, ...]` / etc. — anything with a list-like
    # origin gets split on commas.
    if origin in (list, tuple, set, frozenset):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return raw


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """Dashboard — the home page. First-time users (no media server
    configured) are redirected to the onboarding wizard so they don't
    have to figure out the 40+ field Settings page on day one."""
    from fastapi.responses import RedirectResponse
    if not (settings.media_server_url and settings.media_server_api_key):
        # Skip the redirect if the user explicitly asked for the dashboard
        # via the ?skip_wizard=1 escape hatch (link from the wizard's
        # "I'll configure manually" option).
        if request.query_params.get("skip_wizard") != "1":
            return RedirectResponse(url="/onboarding", status_code=303)

    # Show the update-banner section unconditionally; the in-template
    # logic handles "couldn't check" / "up to date" / "available"
    # rendering. The button-to-execute is gated on update_run_enabled
    # (BABEL_UPDATE_COMMAND env var present) so we pass that flag
    # through here rather than have the template introspect settings.
    from app import updates as updates_mod
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "jobs": jobs.list_jobs(20),
            "server_configured": bool(settings.media_server_url and settings.media_server_api_key),
            "settings": settings.all_values(mask_sensitive=True),
            "active": "dashboard",
            "update_run_enabled": updates_mod.update_run_enabled(),
        },
    )


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(request: Request) -> HTMLResponse:
    """First-run wizard. A focused 3-step form that gets a fresh user
    from "fresh install" to "ready to subtitle their first film"
    without having to navigate the full Settings form.

    The form posts to /onboarding which delegates to the same backend
    update path Settings uses, so there's no duplicated validation
    logic — the wizard is purely a UX shell over /api/settings.
    """
    vals = settings.all_values(mask_sensitive=True)
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "values": vals,
            "already_configured": bool(
                settings.media_server_url and settings.media_server_api_key
            ),
            "active": "dashboard",   # Dashboard nav stays highlighted
        },
    )


@router.post("/onboarding", response_class=HTMLResponse)
async def onboarding_save(request: Request) -> HTMLResponse:
    """Save the wizard's three sections in one shot, then redirect to
    the Library so the user lands on their content. Re-uses the
    SettingsStore.update path so validation is shared with the regular
    Settings form."""
    from fastapi.responses import RedirectResponse
    form = await request.form()
    # Only update the fields the wizard exposes. Trim empties so they
    # don't overwrite existing values with blanks on partial saves.
    updates: dict[str, Any] = {}
    for key in (
        "media_server_type", "media_server_url", "media_server_api_key",
        "default_target_lang", "default_translation_provider",
    ):
        v = form.get(key)
        if v is not None and str(v).strip():
            updates[key] = str(v).strip()
    if updates:
        try:
            settings.update(updates)
        except (ValueError, ValidationError) as e:
            # Re-render the wizard with the error rather than redirecting,
            # so the user sees what went wrong without losing their work.
            return templates.TemplateResponse(
                request,
                "onboarding.html",
                {
                    "values": {**settings.all_values(mask_sensitive=True), **updates},
                    "already_configured": False,
                    "error": str(e),
                    "active": "dashboard",
                },
                status_code=400,
            )
    return RedirectResponse(url="/library", status_code=303)


@router.get("/partials/jobs", response_class=HTMLResponse)
def jobs_partial(request: Request) -> HTMLResponse:
    """HTMX swaps this partial into the dashboard every few seconds for live updates."""
    return templates.TemplateResponse(request, "_jobs_table.html", {"jobs": jobs.list_jobs(20)})


@router.get("/library", response_class=HTMLResponse)
def library(
    request: Request,
    target_lang: str | None = None,
    q: str | None = None,
    missing_only: int = 0,
    start_index: int = 0,
    limit: int = 50,
    library_id: str | None = None,
) -> HTMLResponse:
    """Browse media-server items, filter, and queue per-item subtitling jobs."""
    if not settings.media_server_url or not settings.media_server_api_key:
        return templates.TemplateResponse(
            request, "library.html",
            {
                "active": "library",
                "configured": False,
                "items": [], "total": 0,
                "target_lang": target_lang or settings.default_target_lang,
                "q": q or "",
                "missing_only": bool(missing_only),
                "start_index": 0, "limit": limit,
                "language_options": LANGUAGE_OPTIONS,
                "libraries": [],
                "library_id": library_id or "",
                "error": None,
            },
        )

    target_lang = target_lang or settings.default_target_lang

    error = None
    items: list[dict] = []
    total = 0
    libraries: list[dict] = []
    client = media_server_client()
    # Library list and item list go through the same MediaServerError branch
    # — if the server flakes, the page should still render the filter form
    # (it just won't have a library dropdown populated until next refresh).
    try:
        libs = client.list_libraries()
        libraries = [{"id": l.id, "name": l.name, "type": l.type} for l in libs]
    except (MediaServerError, HTTPException) as e:
        error = str(e)

    if error is None:
        try:
            page = client.list_videos(
                start_index=start_index, limit=limit, search_term=q or None,
                library_id=library_id or None,
            )
            for it in page.items:
                has_sub = it.has_subtitle_track(target_lang)
                if missing_only and has_sub:
                    continue
                items.append({
                    "id": it.id, "name": it.name, "type": it.type,
                    "path": it.path, "has_target_subtitle": has_sub,
                })
            total = page.total
        except (MediaServerError, HTTPException) as e:
            error = str(e)

    return templates.TemplateResponse(
        request, "library.html",
        {
            "active": "library",
            "configured": True,
            "items": items,
            "total": total,
            "target_lang": target_lang,
            "q": q or "",
            "missing_only": bool(missing_only),
            "start_index": start_index,
            "limit": limit,
            "language_options": LANGUAGE_OPTIONS,
            "libraries": libraries,
            "library_id": library_id or "",
            "error": error,
        },
    )


def _find_legacy_pipeline_metrics(output_path: str | None) -> dict | None:
    """Recover pipeline_metrics for a legacy Job (pre-0.7.13, before
    ``Job.pipeline_metrics`` was a persisted field) by scanning the
    VTT cache for a payload whose ``media_path`` plausibly matches
    the job's output filename. Best-effort — None if nothing fits.

    The .vtt filename produced by the runner is shaped as
    ``<media_basename>.<lang>.<mode>.ai.vtt`` (see ``api/manage._vtt_path``).
    We strip the four predictable trailing components to recover the
    media basename, then compare it against the stem of each cached
    payload's ``media_path``. First match wins."""
    if not output_path:
        return None
    import json
    from pathlib import Path
    out_name = Path(output_path).name
    # ``Inception.fr.ai.vtt`` → ``Inception`` after stripping
    # ``.vtt`` → ``.ai`` → ``.<lang>``. Pre-0.7.32 the filename also
    # had a ``.<mode>`` infix (``.audio``/``.scene``/``.cinematic``)
    # between ``.<lang>`` and ``.ai``; we still strip those when
    # present so older .vtt files in the user's library keep being
    # discoverable by this lookup.
    base = out_name
    if base.endswith(".vtt"):
        base = base[:-4]
    if base.endswith(".ai"):
        base = base[:-3]
    for legacy_mode in (".audio", ".scene", ".cinematic"):
        if base.endswith(legacy_mode):
            base = base[: -len(legacy_mode)]
            break
    # ``.<lang>`` — at most 5 chars (e.g. ".fr", ".zh-CN"); we just
    # peel one final ``.<word>`` segment off.
    if "." in base:
        base = base.rsplit(".", 1)[0]
    if not base:
        return None

    cache_root = Path(settings.cache_dir)
    if not cache_root.is_dir():
        return None
    for entry in cache_root.iterdir():
        if not entry.is_file() or entry.suffix != ".json":
            continue
        if entry.name in {"settings.json", "jobs.json"}:
            continue
        try:
            payload = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        media_path = payload.get("media_path") or ""
        if base in Path(media_path).stem:
            pm = payload.get("pipeline_metrics")
            if isinstance(pm, dict):
                return pm
    return None


@router.get("/jobs/{job_id}/error", response_class=HTMLResponse)
def job_error_page(request: Request, job_id: str) -> HTMLResponse:
    """Full error detail page for a failed job — linked from the "▸ error"
    pill in the Jobs table's Error column.

    Renders the short error message AND the full Python traceback the
    runner captured at failure time. Pre-0.7.25 records don't have a
    traceback (we didn't capture one); they fall back to showing only
    the short error string with a note explaining why."""
    from fastapi import HTTPException
    from app import jobs as jobs_mod
    j = jobs_mod.get_job(job_id)
    if not j:
        raise HTTPException(404, f"job {job_id!r} not found")
    if j.status != "failed":
        # Not failed — there's no error to show. Bounce back to the
        # dashboard rather than render an empty page.
        return HTMLResponse(
            status_code=400,
            content=(
                f"<p>Job <code>{job_id}</code> has status "
                f"<strong>{j.status}</strong>, not <em>failed</em>. "
                f"<a href='/'>Back to dashboard</a>.</p>"
            ),
        )
    return templates.TemplateResponse(
        request,
        "job_error.html",
        {
            "job": j,
            "active": "dashboard",
        },
    )


@router.get("/jobs/{job_id}/stats", response_class=HTMLResponse)
def job_stats_page(request: Request, job_id: str) -> HTMLResponse:
    """Per-job version of the stats page — renders the same template
    the Cache Explorer uses, but reads the .vtt straight from the
    job's recorded output_path (no cache_key lookup needed). Linked
    from the Quality pill in the Jobs table so the user can drill
    into "why is this run a B and not an A" without having to find
    the matching Cache Explorer row by hand."""
    from fastapi import HTTPException
    from pathlib import Path
    from app import jobs as jobs_mod
    from app import stats as stats_mod
    j = jobs_mod.get_job(job_id)
    if not j:
        raise HTTPException(404, f"job {job_id!r} not found")
    if not j.output_path:
        raise HTTPException(
            404,
            f"job {job_id!r} produced no .vtt yet (still running / failed / canceled)",
        )
    path = Path(j.output_path)
    if not path.is_file():
        raise HTTPException(
            404,
            f"output file {path.name!r} no longer exists on disk",
        )
    try:
        vtt_text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"could not read output: {e}")

    # Pass through the job's stored pipeline_metrics so the score the
    # page renders is byte-identical to the score in the Jobs table's
    # Quality pill. Without this the page recomputes from the .vtt
    # alone, misses every VAD / packing / translation penalty, and
    # silently reports a higher score than the pill claimed.
    #
    # Fallback for legacy jobs (pre-0.7.13, before Job.pipeline_metrics
    # was a field): search the VTT cache for a payload whose
    # ``media_path`` matches this job's output. The .vtt name carries
    # the media basename (``<basename>.<lang>.<mode>.ai.vtt``), so we
    # peel off the predictable suffixes to recover the basename and
    # match it against cached ``media_path`` stems. Linear scan over
    # cache_dir/*.json — typically a handful of files, dwarfed by the
    # template render cost.
    pipeline_metrics = getattr(j, "pipeline_metrics", None)
    if pipeline_metrics is None:
        pipeline_metrics = _find_legacy_pipeline_metrics(j.output_path)

    record = stats_mod.compute_from_vtt(
        vtt_text,
        media_path=str(path),
        cache_key=f"job:{job_id}",
        mode=j.mode,
        pipeline_metrics=pipeline_metrics,
    )
    return templates.TemplateResponse(
        request,
        "cache_stats.html",
        {
            "stats": record,
            "cache_key": f"job:{job_id}",
            "active": "dashboard",
        },
    )


@router.get("/cache/vtt/{cache_key}/stats", response_class=HTMLResponse)
def cache_stats_page(request: Request, cache_key: str) -> HTMLResponse:
    """Render the per-entry quality/coverage stats page. Same numbers
    the API endpoint returns, formatted as a human-readable layout with
    duration histogram + per-10-min coverage table. Linked from the
    Cache Explorer's 📊 button on every row."""
    from fastapi import HTTPException
    import json
    from pathlib import Path
    from app import cache_explorer as ce
    from app import stats as stats_mod

    try:
        ce._validate_cache_key(cache_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    path = Path(settings.cache_dir) / f"{cache_key}.json"
    if not path.is_file():
        raise HTTPException(404, f"cache entry {cache_key!r} not found")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise HTTPException(500, "unreadable cache entry")
    vtt_text = payload.get("vtt", "") if isinstance(payload, dict) else ""
    record = stats_mod.compute_from_vtt(
        vtt_text,
        media_path=payload.get("media_path") if isinstance(payload, dict) else None,
        cache_key=cache_key,
        mode=payload.get("mode") if isinstance(payload, dict) else None,
        detected_source_language=(
            payload.get("detected_source_language") if isinstance(payload, dict) else None
        ),
        pipeline_metrics=(
            payload.get("pipeline_metrics") if isinstance(payload, dict) else None
        ),
    )
    # Reference comparison (0.9.0): pull the cached score lazily —
    # recomputes against the current VTT if the user re-polished
    # since the original upload. None means "no reference uploaded
    # yet" and the template renders an upload form instead of the
    # score panel.
    reference_score = None
    if isinstance(payload, dict) and vtt_text:
        try:
            from app.reference_store import maybe_recompute_score
            import re as _re
            m = _re.search(
                r"NOTE Subtitle This auto-subs \([a-z]{2} -> (?P<tgt>[a-z]{2})",
                vtt_text,
            )
            if m:
                reference_score = maybe_recompute_score(
                    cache_key, vtt_text, vtt_target_lang=m.group("tgt"),
                )
        except Exception:
            # Reference scoring is observability — must never block the
            # stats page rendering for the heuristic Quality Score etc.
            pass
    return templates.TemplateResponse(
        request,
        "cache_stats.html",
        {
            "stats": record,
            "cache_key": cache_key,
            "reference_score": reference_score,
            "active": "cache",
        },
    )


@router.get("/cache", response_class=HTMLResponse)
def cache_explorer_page(request: Request) -> HTMLResponse:
    """The Cache Explorer page. Lists every VTT (result) cache entry and
    every transcript (STT) cache entry side by side, with per-row delete
    buttons so the user can force a re-run on a specific film without
    SSH-ing into the host to find the right hashed filename."""
    from app import cache_explorer as ce
    return templates.TemplateResponse(
        request,
        "cache_explorer.html",
        {
            "vtt_entries": ce.list_vtt_entries(),
            "transcript_entries": ce.list_transcript_entries(),
            "active": "cache",
        },
    )


def _field_warnings() -> dict[str, str]:
    """Per-field runtime warnings rendered inline on the Settings page.
    Currently only one entry — vocal_isolation_enabled flags "demucs
    is on but not installed in this image" so the user fixes it from
    the form instead of by submitting a doomed job.

    Each entry is keyed by field name; absence means no warning.
    Cheap probe — just a try-import — so re-evaluating per page render
    is fine."""
    warnings: dict[str, str] = {}
    if settings.vocal_isolation_mode != "off":
        from app.pipeline import vocal_isolation as vi
        ok, err = vi.is_available()
        if not ok:
            warnings["vocal_isolation_mode"] = (
                "demucs is NOT usable in this image. Any job submitted "
                "while this is enabled will fail fast at queue time. "
                "Fix: if you run the GHCR image, `docker compose pull "
                "&& docker compose up -d` (every image from 0.7.27 "
                "onward exposes a working vocal-isolation path). If "
                "you build your own image, ensure `demucs>=4.0` is "
                "installed and that `from demucs.pretrained import "
                "get_model` works. Otherwise, set this back to OFF."
            )
    return warnings


def _auto_improvements() -> list[dict]:
    """Pipeline improvements that run AUTOMATICALLY when conditions
    are right. None of them has a setting to flip — the banner at
    the top of the Settings page lists them so the user sees what's
    already being done.

    ``active`` indicates whether the current backend / source config
    will actually exercise the feature; inactive entries render
    dimmed so the list is honest about what's running NOW vs what
    requires a different choice elsewhere in Settings."""
    backend = settings.whisper_backend.lower()
    is_cpu_backend = backend == "cpu"
    return [
        {
            "name": "Center-channel extraction",
            "detail": "5.1+ sources → ffmpeg pan=mono|c0=FC (dialogue-only audio)",
            "active": True,
        },
        {
            "name": "Loudness normalization",
            "detail": "EBU R128 single-pass to −23 LUFS — brings audio into Whisper's training range",
            "active": True,
        },
        {
            "name": "Anti-hallucination filter",
            "detail": "drops YouTube-tail signature phrases + n-gram stuck loops post-STT",
            "active": True,
        },
        {
            "name": "Confidence-gated re-transcription",
            "detail": (
                "re-decodes weak 10-min buckets with aggressive params (cpu backend only)"
                if is_cpu_backend
                else "requires Whisper backend = cpu (OpenVINO can't surface avg_logprob)"
            ),
            "active": is_cpu_backend,
        },
        {
            "name": "Word-level timestamps",
            "detail": (
                "DTW alignment for frame-accurate per-word timing (cpu backend only)"
                if is_cpu_backend
                else "requires Whisper backend = cpu"
            ),
            "active": is_cpu_backend,
        },
        {
            "name": "Orphan-word line breaks",
            "detail": "VTT writer avoids ending a line on \"of\", \"the\", \"de\", \"la\", …",
            "active": True,
        },
    ]


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "sections": _section_groups(),
            "values": settings.all_values(mask_sensitive=True),
            "sensitive": SENSITIVE_FIELDS,
            "read_only": READ_ONLY_FIELDS,
            "field_warnings": _field_warnings(),
            "auto_improvements": _auto_improvements(),
            "active": "settings",
            "saved": False,
        },
    )


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request) -> HTMLResponse:
    form = await request.form()
    payload: dict[str, Any] = {}
    valid_keys = {f["key"] for f in _FIELD_META}
    bool_keys = {f["key"] for f in _FIELD_META if f["type"] == "checkbox"}

    # Checkboxes that aren't checked don't appear in form data — set them to False explicitly.
    for k in bool_keys:
        payload[k] = k in form

    for k, raw in form.items():
        if k not in valid_keys or k in bool_keys:
            continue
        if k in SENSITIVE_FIELDS and raw == "":
            continue   # don't blank an already-set secret on resubmit
        payload[k] = _coerce(k, str(raw))

    error = None
    try:
        settings.update(payload)
    except ValueError as e:
        error = str(e)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "sections": _section_groups(),
            "values": settings.all_values(mask_sensitive=True),
            "sensitive": SENSITIVE_FIELDS,
            "read_only": READ_ONLY_FIELDS,
            "field_warnings": _field_warnings(),
            "auto_improvements": _auto_improvements(),
            "active": "settings",
            "saved": error is None,
            "error": error,
        },
    )
