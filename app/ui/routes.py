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
from app.processor import SUPPORTED_MODES
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
#   [+0 LLM calls]            no extra LLM cost over baseline
#   [+~20 vision calls/film]  scene mode adds ~20 calls per film
#   [ANY HOST · slow]         no special hardware needed but slow
#   [INTEL iGPU · 5-10× faster]  needs specific hardware, much faster
#   [~500 MB · balanced]      disk footprint + speed/quality trade-off
#
# Whisper choices have NO $ cost — only compute time × disk space.

_SECTION_META: dict[str, str] = {
    "Media server": (
        "START HERE — without a working media server connection nothing else "
        "is reachable. Pick your server type, paste its URL + API key (X-Plex-"
        "Token for Plex), save. The Library tab lights up once this section "
        "is configured."
    ),
    "Defaults": (
        "Pre-set choices applied when you click 'Subtitle this' or 'Subtitle "
        "selected' in the Library without overrides. The cost/complexity "
        "lever is Mode here — pick the provider in the Translation section "
        "below."
    ),
    "Speech-to-Text": (
        "Whisper transcribes audio to text. ALWAYS FREE — runs 100% locally, "
        "model is downloaded once. The trade-off here is compute time × quality "
        "× disk space, NOT money."
    ),
    "Translation": (
        "Pick the translation provider, then configure its specific knobs. "
        "Provider is the main cost/quality lever — NLLB is fully free and "
        "local, DeepL is freemium cloud, LLM uses whatever you wire up in the "
        "Translation model section below (local Ollama, cloud Claude / GPT, "
        "anything in between)."
    ),
    "Translation model": (
        "The LLM that translates subtitle cues (only used when provider=llm). "
        "Configure cloud (Anthropic / OpenAI / OpenRouter / …) or fully local "
        "(Ollama / LM Studio / LocalAI / vLLM) — Subtitle This doesn't care which."
    ),
    "Vision model": (
        "The LLM that describes keyframes for the scene bible used by scene "
        "and cinematic modes."
    ),
    "Scene & Cinematic": (
        "Tuning knobs for scene-detection and cinematic-frame extraction."
    ),
    "Subtitles": (
        "WebVTT line-wrap formatting."
    ),
    "Resource safety": (
        "Caps that keep a single job from consuming the host. The defaults are "
        "sized for a 2 h film on a 6 GB / 4 vCPU container. Combine these with "
        "the cgroup limits in docker-compose.yml — the kernel limits are the "
        "actual fence; the in-process caps reduce the chance of ever hitting it."
    ),
    "Security": (
        "Optional HTTP Basic auth in front of the whole app. OFF by default "
        "for the zero-config first-boot experience. Turn ON on any network "
        "where you wouldn't trust every device to start jobs or read your "
        "API keys."
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
    # The Vision model section drives the scene bible builder. Only invoked
    # by scene/cinematic modes — hide it when the user is on audio mode.
    "Vision model": {"field": "default_mode",
                     "equals": ["scene", "cinematic"]},
    # Scene-detection tuning knobs do nothing in audio mode.
    "Scene & Cinematic": {"field": "default_mode",
                          "equals": ["scene", "cinematic"]},
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
         {"value": "emby",
          "label": "emby — Emby Server (the original)"},
         {"value": "jellyfin",
          "label": "jellyfin — Jellyfin (open-source fork of Emby; same REST API)"},
         {"value": "plex",
          "label": "plex — Plex Media Server (different API + auth, uses X-Plex-Token)"},
     ],
     "help": "Which media server you're talking to. Emby and Jellyfin share an "
             "implementation (their REST APIs are functionally identical — Jellyfin keeps "
             "Emby's /Items, /System/Info/Public endpoints and the X-Emby-Token auth header). "
             "Plex has its own client (X-Plex-Token auth, /library/sections + "
             "/library/metadata/{ratingKey} endpoints)."},
    {"key": "media_server_url", "section": "Media server",
     "label": "Server URL", "type": "text",
     "help": "Where Subtitle This reaches your media server. Examples: "
             "http://emby:8096 (docker-compose service name), "
             "http://jellyfin:8096, "
             "http://plex:32400 (Plex's default port), "
             "or http://192.168.1.10:8096 (LAN IP)."},
    {"key": "media_server_api_key", "section": "Media server",
     "label": "API key (Plex: X-Plex-Token)", "type": "password",
     "help": "For Emby: generate at Emby admin → Server Settings → Advanced → API Keys. "
             "For Jellyfin: same path — Dashboard → API Keys. "
             "For Plex: this is your X-Plex-Token (find it on plex.tv/account → "
             "Authorized Devices, or sign in once and grab it from any local-server URL "
             "in your browser)."},
    {"key": "media_server_verify_ssl", "section": "Media server",
     "label": "Verify SSL certificate (TLS)", "type": "checkbox",
     "help": "Leave ON when your Server URL is plain http:// (the toggle is ignored) "
             "OR when it's https:// with a publicly-trusted certificate (Let's Encrypt "
             "behind Caddy/Nginx, etc.). Turn OFF for: Plex accessed via LAN IP (the "
             "bundled cert is for *.plex.direct so the hostname doesn't match), "
             "Emby/Jellyfin behind a self-signed cert, or any homelab reverse proxy "
             "without a CA-issued cert. Disabling verification means an attacker on "
             "your network could MITM the traffic between this container and the media "
             "server — only do it on a trusted LAN. Advanced alternative: keep this ON "
             "and mount a custom CA bundle into the container, then set "
             "SSL_CERT_FILE=/path/to/ca.crt in the env — httpx picks it up automatically."},

    # ── Defaults — workflow knobs ─────────────────────────────────────────────
    # Per-job overrides for target language, mode, and skip behavior. The
    # provider chooser used to live here but moved into the Translation
    # section below — it belongs with the knobs it gates.
    {"key": "default_target_lang", "section": "Defaults",
     "label": "Default target language", "type": "select",
     "options": _LANGUAGE_DROPDOWN_OPTIONS,
     "help": "Language you want subtitles in. Used when you don't override per-job. "
             "Coverage varies by translation provider — NLLB and DeepL support ~30 "
             "languages well; LLM providers handle whatever the underlying model knows."},
    # NOTE: default_source_lang_priority is intentionally not exposed.
    # Hard-coded default ['en', '*'] in _EnvSettings handles 99% of cases.
    # Power users can override via BABEL_DEFAULT_SOURCE_LANG_PRIORITY.
    # NOTE: default_translation_provider lives in the Translation section
    # below — the provider chooser belongs with the knobs it gates.
    {"key": "default_mode", "section": "Defaults",
     "label": "Quality tier — pick your call-volume tier", "type": "select",
     "options": [
         {"value": "audio",
          "label": "audio · [+0 LLM calls beyond translation] whisper only · works with any provider · cheapest · default"},
         {"value": "scene",
          "label": "scene · [+~20 vision-LLM calls/film] adds scene bible · improves pronouns/gender · needs Vision model + provider=llm"},
         {"value": "cinematic",
          "label": "cinematic · [+1 LLM call per cue with image] adds per-cue frame to translation · most expensive · needs vision-capable Translation model"},
     ],
     "help": (
         "Higher tier = more visual context for the translator = better quality on tricky "
         "scenes, but more LLM calls.\n"
         "• audio uses Whisper only. No vision. Works with any provider (NLLB/DeepL/LLM).\n"
         "• scene runs ffmpeg scene-detection, sends one keyframe per shot to the Vision LLM "
         "for a 1-2 sentence description, then feeds the resulting bible to the translator as "
         "cached system context. Requires Vision model section configured AND provider=llm.\n"
         "• cinematic does what scene does AND additionally attaches one keyframe per cue to "
         "the translation call so the translator literally sees each moment. Requires the "
         "Translation model to be vision-capable AND provider=llm."
     )},
    {"key": "default_skip_if_target_audio_exists", "section": "Defaults",
     "label": "Skip when target-language audio is already present", "type": "checkbox",
     "help": "If the file already has audio in the target language, do nothing. Saves "
             "compute on items where the user can just switch audio track in their player."},
    {"key": "write_detected_language_to_file", "section": "Defaults",
     "label": "Tag detected source language back into the source file (MKV only)", "type": "checkbox",
     "help": "When a film's audio track has no language tag (Emby just shows 'Audio'), "
             "language detection runs differently per backend: the OpenVINO STT backend "
             "needs a Whisper-tiny pre-pass on the first 30s of audio (it can't surface "
             "its own auto-detection through the optimum-intel API), while the CPU/"
             "faster-whisper backend detects internally during the main transcribe call "
             "at zero extra cost. Either way the transcription gets the right language. "
             "With this checkbox ON, we ALSO write the detected language back into the "
             "file's EBML header via `mkvpropedit` — instant, modifies only metadata, "
             "NEVER touches the audio/video data sections. Restricted to MKV/MKA/WebM. "
             "Non-Matroska containers (MP4/MOV/AVI/...) are deliberately left untouched: "
             "an ffmpeg remux would technically preserve audio byte-for-byte but rewrites "
             "the whole file with documented edge cases (timestamp re-derivation, lost "
             "custom metadata) — not worth the risk on a media library. Detection still "
             "drives transcription correctness regardless of container; only the persist-"
             "to-Emby step is skipped for non-MKV. Turn off entirely to keep all source "
             "files completely pristine."},

    # ── Vocal isolation (Demucs) ──────────────────────────────────────────────
    {"key": "vocal_isolation_enabled", "section": "Speech-to-Text",
     "label": "Vocal isolation before STT (Demucs)",
     "type": "checkbox",
     "help": "Splits the source audio into stems and keeps only the "
             "VOCALS stem before feeding it to Whisper. The score, "
             "ambience, and SFX are removed so Whisper transcribes a "
             "clean speech signal. Closes most of the 'climax dialog "
             "buried under music' gap on action/sci-fi films "
             "(Inception, Dunkirk, Tenet). \n\n"
             "Costs: an extra 8-30 min of CPU per 2 h film (Demucs "
             "runs ~4-10× realtime on a 4-core container) and "
             "requires the optional `demucs` package in the image "
             "(`pip install demucs>=4.0`). The model runs as a "
             "distinct phase BEFORE Whisper loads — when STT starts, "
             "Demucs has already released its ~1 GB of RAM. \n\n"
             "Heuristic for when to turn this on: continuous loud "
             "score that drowns dialogue. Marginal on dialog-driven "
             "dramas with sparse music."},
    {"key": "vocal_isolation_model", "section": "Speech-to-Text",
     "label": "Demucs model", "type": "select",
     "show_if": {"field": "vocal_isolation_enabled", "equals": "true"},
     "options": [
         {"value": "htdemucs",
          "label": "htdemucs · [~80 MB · 4-stem · best quality · slowest] separates vocals + drums + bass + other"},
         {"value": "mdx_extra_q",
          "label": "mdx_extra_q · [quantized · 2-stem · faster · lighter] vocals vs no_vocals"},
     ],
     "help": "htdemucs is the default 4-stem model — best general "
             "quality, separates drums/bass/other/vocals. "
             "mdx_extra_q is a quantized 2-stem variant that's about "
             "30% faster with slightly lower fidelity on the vocals "
             "stem. Either downloads to HF_HOME on first run."},

    # ── Speech-to-Text ────────────────────────────────────────────────────────
    {"key": "whisper_backend", "section": "Speech-to-Text",
     "label": "Backend", "type": "select",
     "options": [
         {"value": "cpu",
          "label": "cpu · [ANY HOST · slow] faster-whisper · 20–60 min for a 2h film on small/medium model"},
         {"value": "openvino",
          "label": "openvino · [INTEL iGPU · 5–10× faster] needs N305/N100/etc. host + openvino-flavored image"},
     ],
     "help": (
         "• cpu uses faster-whisper, runs entirely on the CPU. INT8 quantization keeps it "
         "tractable but slow. Works on any host.\n"
         "• openvino exports Whisper to OpenVINO IR and runs inference on the Intel iGPU. "
         "Only works in the openvino-flavored image with /dev/dri exposed (TrueNAS Scale "
         "with N305/N100/iGPU-equipped Intel host).\n"
         "Note even with openvino, several pipeline steps stay CPU-bound: ffmpeg audio "
         "extraction, the language-detection pre-pass (faster-whisper-tiny on untagged "
         "audio), and the FIRST run's IR conversion (5-30 min, one-off). 100% CPU during "
         "those phases is normal."
     )},
    {"key": "whisper_model", "section": "Speech-to-Text", "label": "Whisper model", "type": "select",
     "options": [
         {"value": "tiny",           "label": "tiny · [~75 MB · fastest · low quality] for quick smoke tests only"},
         {"value": "base",           "label": "base · [~150 MB · fast · ok quality]"},
         {"value": "small",          "label": "small · [~500 MB · balanced · good quality] ← default"},
         {"value": "medium",         "label": "medium · [~1.5 GB · slow · great quality]"},
         {"value": "large-v3",       "label": "large-v3 · [~3 GB · slowest · best quality]"},
         {"value": "large-v3-turbo", "label": "large-v3-turbo · [~1.5 GB · fast for size · near-best quality, ~2× faster than large-v3]"},
     ],
     "help": "Larger = better but slower and more disk. All sizes are free and local — "
             "Whisper has no API cost. First-time use of a model triggers a one-off "
             "download to /cache."},
    {"key": "whisper_compute_type", "section": "Speech-to-Text",
     "label": "Compute type", "type": "select",
     "show_if": {"field": "whisper_backend", "equals": "cpu"},
     "options": [
         {"value": "int8",    "label": "int8 · [fastest · lowest precision] default — works well in practice"},
         {"value": "int16",   "label": "int16 · [fast · slightly more precise]"},
         {"value": "float16", "label": "float16 · [slow · high precision]"},
         {"value": "float32", "label": "float32 · [slowest · full precision · rarely worth it]"},
     ],
     "help": "Quantization for faster-whisper. Lower precision = faster + less RAM, "
             "with negligible quality loss for subtitle work."},
    {"key": "whisper_device", "section": "Speech-to-Text",
     "label": "Device", "type": "select",
     "show_if": {"field": "whisper_backend", "equals": "cpu"},
     "options": [
         {"value": "cpu",  "label": "cpu · [universal] works on any host"},
         {"value": "cuda", "label": "cuda · [NVIDIA GPU] needs nvidia-container-toolkit (rare on TrueNAS)"},
     ],
     "help": "Where faster-whisper runs. cuda only matters if you've added an NVIDIA GPU."},
    {"key": "openvino_device", "section": "Speech-to-Text",
     "label": "OpenVINO device", "type": "select",
     "show_if": {"field": "whisper_backend", "equals": "openvino"},
     "options": [
         {"value": "AUTO", "label": "AUTO · [recommended] picks GPU when available, falls back to CPU silently"},
         {"value": "GPU",  "label": "GPU · force Intel iGPU — fails loudly if /dev/dri or driver isn't available"},
         {"value": "CPU",  "label": "CPU · force CPU even on iGPU hosts (useful for benchmarking)"},
     ],
     "help": (
         "Where OpenVINO runs inference (Whisper STT and NLLB translation). "
         "AUTO is the right default but silently falls back to CPU if it can't "
         "use the GPU — switch to GPU explicitly to surface the real reason. "
         "Watch `docker logs subtitle-this` after a model load: the line "
         "'[openvino] whisper:…  selected=GPU' confirms what was actually picked."
     )},
    {"key": "stt_region_packing", "section": "Speech-to-Text",
     "label": "Region packing (OpenVINO) — fast mode", "type": "checkbox",
     "show_if": {"field": "whisper_backend", "equals": "openvino"},
     "help": "ON (default, fast): groups many short speech bits into a single "
             "Whisper transcription pass. Drastically faster — a 2 h film "
             "takes ~10 minutes on an Intel iGPU. The trade-off is that "
             "Whisper can occasionally lose track of timing when too many "
             "bits are bundled, which used to lose dialog. Mitigated since "
             "0.7.11 by the density cap (see field below) and snap recovery; "
             "in practice ON now produces near-OFF accuracy on most films.\n"
             "\n"
             "OFF (slow, max accuracy): each speech segment gets its own "
             "Whisper pass. Same 2 h film now takes ~1.5-2 hours of "
             "transcription on the same iGPU — roughly 10× slower. Turn OFF "
             "only when ON is still missing visible dialog after tuning the "
             "density cap.\n"
             "\n"
             "Ignored on the CPU/faster-whisper backend (it has its own "
             "longform batching)."},
    {"key": "stt_max_regions_per_window", "section": "Speech-to-Text",
     "label": "Region packing — max regions per pass", "type": "number",
     "show_if": {"field": "whisper_backend", "equals": "openvino"},
     "help": "Hard cap on how many short speech bits get bundled into one "
             "Whisper pass. Lower = more accurate timing (less risk of "
             "losing dialog), slower transcription. Higher = faster, more "
             "risk.\n"
             "\n"
             "Reference points: 4 (default, recommended), 8 (faster, "
             "still safe with snap recovery), 0 (no cap — legacy "
             "pre-0.7.11 behavior, drops ~40 % of dialog on dense films). "
             "Most users should leave this alone."},

    # ── Translation (provider chooser + provider-specific params) ─────────────
    # The provider chooser lives at the top of this section so it gates the
    # NLLB/DeepL/LLM-specific knobs that follow. The translation_batch_size
    # field (LLM-only) lives in the Translation model section instead — it's
    # part of the LLM config block, not the provider block.
    {"key": "default_translation_provider", "section": "Translation",
     "label": "Translation provider — pick your cost tier", "type": "select",
     "options": [
         {"value": "nllb",
          "label": "nllb · [FREE · LOCAL] Meta NLLB-200 · 200 langs · works on both image flavors · ~1.5 GB downloaded on first call"},
         {"value": "deepl",
          "label": "deepl · [FREE TIER 500k chars/mo · CLOUD beyond] best on EU/Asian pairs · ~30 langs · text-only"},
         {"value": "llm",
          "label": "llm · [VARIES] uses LLM configured below · free if local (Ollama) or paid if cloud · best quality · vision-aware in scene/cinematic"},
     ],
     "help": (
         "Sorted from cheapest to most flexible:\n"
         "• NLLB — fully free, local, no account, no key. Decent quality on ~30 well-supported "
         "language pairs. Works on both image flavors (uses Intel iGPU via OpenVINO when "
         "available, falls back to CPU torch otherwise — slower but no setup either way).\n"
         "• DeepL — free 500k characters/month (~6 movies), then paid. Excellent quality on "
         "European and East-Asian pairs. The DeepL API key field appears below when you pick this.\n"
         "• LLM — uses whatever you configure in the Translation model section. Highest "
         "quality, supports any language pair. Free if you point at local Ollama / LM Studio. "
         "Paid per-token if you point at Anthropic / OpenAI / OpenRouter / etc. The only "
         "provider that benefits from scene/cinematic visual context."
     )},
    {"key": "nllb_model", "section": "Translation",
     "label": "NLLB model variant", "type": "select",
     "show_if": {"field": "default_translation_provider", "equals": "nllb"},
     "options": [
         {"value": "facebook/nllb-200-distilled-600M",
          "label": "distilled-600M · [~1.5 GB · balanced] default · good quality · fast"},
         {"value": "facebook/nllb-200-distilled-1.3B",
          "label": "distilled-1.3B · [~3 GB · better] noticeably better fluency · 2× slower"},
         {"value": "facebook/nllb-200-1.3B",
          "label": "1.3B · [~5 GB · alternative] non-distilled — slightly different quality profile"},
         {"value": "facebook/nllb-200-3.3B",
          "label": "3.3B · [~7 GB · best · slow] highest quality · needs ~16 GB RAM · very slow on CPU"},
     ],
     "help": "Meta NLLB-200 model size. Bigger = better translations but slower and more RAM. "
             "First use of a variant downloads the weights (one-off, cached in /cache/nllb-models)."},
    {"key": "nllb_batch_size", "section": "Translation",
     "label": "Cues per NLLB batch", "type": "number",
     "show_if": {"field": "default_translation_provider", "equals": "nllb"},
     "help": "Only used when provider=NLLB. Higher = fewer model.generate() calls, "
             "lower = less peak RAM per call (the KV cache scales with batch × "
             "seq_len). Default 4 is tuned conservatively for NLLB-1.3B + a 12 GB "
             "cgroup with Whisper-large page cache lingering; bump to 8-16 if you "
             "use the 600M variant or have more memory headroom."},
    {"key": "nllb_load_in_8bit", "section": "Translation",
     "label": "Compress NLLB weights to int8 (OpenVINO path)", "type": "checkbox",
     "show_if": {"field": "default_translation_provider", "equals": "nllb"},
     "help": "Halves resident weight memory by quantizing to int8 via NNCF at "
             "load time (~3 GB → ~1.5 GB for distilled-1.3B). First-time load "
             "pays a 1-2 min quantization cost; the result is cached on disk. "
             "Quality cost is roughly 0.3 BLEU — below the noise floor for "
             "subtitle work. Default ON because the 1.3B variant otherwise "
             "doesn't fit in 12 GB of cgroup alongside Whisper's page cache. "
             "Turn OFF only if you have 16+ GB headroom and want strict "
             "full-precision weights. No effect on the CPU/torch fallback path."},
    {"key": "deepl_batch_size", "section": "Translation",
     "label": "Cues per DeepL request", "type": "number",
     "show_if": {"field": "default_translation_provider", "equals": "deepl"},
     "help": "Only used when provider=DeepL. DeepL caps a single request at 50 "
             "texts, so this is also the upper bound. Lower it for more granular "
             "retry behavior at the cost of more round-trips."},
    {"key": "deepl_api_key", "section": "Translation",
     "label": "DeepL API key", "type": "password",
     "show_if": {"field": "default_translation_provider", "equals": "deepl"},
     "help": "Required when provider = DeepL. Free-tier keys end in ':fx' "
             "(auto-detected — Babel routes to api-free.deepl.com vs api.deepl.com). "
             "Sign up at https://www.deepl.com/pro-api — Free plan gives 500k chars/month."},

    # ── Translation model (only used when provider=llm) ───────────────────────
    {"key": "translation_batch_size", "section": "Translation model",
     "label": "Cues per LLM batch", "type": "number",
     "help": "Higher = fewer round-trips, lower = more granular failures and retries. "
             "30 is a good balance. Only affects the LLM provider — NLLB and DeepL have "
             "their own batch sizes in the Translation section above."},
    {"key": "translation_llm_type", "section": "Translation model",
     "label": "Wire protocol", "type": "select",
     "options": [
         {"value": "openai_compat",
          "label": "openai_compat · [universal] OpenAI · Ollama · LM Studio · LocalAI · OpenRouter · Together · Groq · DeepSeek · Zhipu · Gemini-compat · vLLM · llama.cpp"},
         {"value": "anthropic",
          "label": "anthropic · [Claude only] adds prompt caching, adaptive thinking, strict JSON-schema enforcement"},
     ],
     "help": "Pick `openai_compat` for everything except Claude — it's the universal Chat-"
             "Completions protocol and covers all local servers + most cloud providers. Pick "
             "`anthropic` ONLY when the Model field is a Claude variant (you get extra "
             "Anthropic-only features that way)."},
    {"key": "translation_llm_model", "section": "Translation model",
     "label": "Model", "type": "text",
     "help": "What makes a good translator: large parameter count, broad multilingual "
             "training, strong instruction-following.\n"
             "• Frontier cloud (paid): claude-opus-4-7, gpt-4o, gemini-1.5-pro, mistral-large.\n"
             "• Frontier open-source (free if local): qwen2.5:72b, deepseek-v3, llama3.1:70b, "
             "glm-4-flash, command-r-plus.\n"
             "• Cheap & fast: claude-haiku-4-5, gpt-4o-mini, qwen2.5:14b, llama3.1:8b, gemma2:9b."},
    {"key": "translation_llm_endpoint", "section": "Translation model",
     "label": "Endpoint URL (only when wire protocol = openai_compat)", "type": "text",
     "help": "Ignored when wire protocol = anthropic.\n"
             "• Cloud: https://api.openai.com/v1 · https://openrouter.ai/api/v1 · "
             "https://api.deepseek.com/v1 · https://open.bigmodel.cn/api/paas/v4 (Zhipu) · "
             "https://generativelanguage.googleapis.com/v1beta/openai (Gemini-compat).\n"
             "• Local (no API key needed): http://ollama:11434/v1 · http://lmstudio:1234/v1 · "
             "http://localai:8080/v1 · http://host.docker.internal:1234/v1 (LM Studio on the "
             "host machine when Babel runs in Docker)."},
    {"key": "translation_llm_api_key", "section": "Translation model",
     "label": "API key (LEAVE BLANK for local servers)", "type": "password",
     "help": "REQUIRED for cloud providers (Anthropic, OpenAI, OpenRouter, Together, Groq, "
             "DeepSeek, Zhipu, Gemini, …). LEAVE BLANK for local servers (Ollama, LM Studio, "
             "LocalAI) that don't authenticate by default — Babel substitutes a placeholder so "
             "the OpenAI SDK is happy. Set a value only if you've explicitly enabled auth on "
             "your local server (e.g. vLLM with --api-key)."},
    {"key": "translation_llm_supports_vision", "section": "Translation model",
     "label": "Supports vision (required for cinematic mode)", "type": "checkbox",
     "help": "Whether this model accepts image inputs. Cinematic mode attaches one frame "
             "per cue to translation calls — needs a multimodal model (claude-opus-4-7, "
             "gpt-4o, gemini-1.5-pro, qwen2.5-vl, llava, etc.). Anthropic models always "
             "support vision (this flag is ignored when wire protocol = anthropic)."},

    # ── Vision model (only used by scene + cinematic modes) ───────────────────
    {"key": "vision_llm_enabled", "section": "Vision model",
     "label": "Enable scene/cinematic modes", "type": "checkbox",
     "help": "Master switch. Toggle off if you don't have a vision-capable LLM and only "
             "use audio mode. When off, scene/cinematic modes 400 immediately at submission."},
    {"key": "vision_llm_type", "section": "Vision model",
     "label": "Wire protocol", "type": "select",
     "options": [
         {"value": "openai_compat",
          "label": "openai_compat · [universal] Ollama · LM Studio · OpenAI · OpenRouter · Zhipu/GLM · …"},
         {"value": "anthropic",
          "label": "anthropic · [Claude only] adds prompt caching, JSON schema"},
     ],
     "help": "Same convention as the Translation model: pick `openai_compat` for everything "
             "except Claude."},
    {"key": "vision_llm_model", "section": "Vision model",
     "label": "Model", "type": "text",
     "help": "What makes a good vision describer: strong OCR (read on-screen text), "
             "scene-understanding (count/identify characters, recognize settings), and "
             "concise output.\n"
             "• Frontier cloud: claude-opus-4-7, gpt-4o, gemini-1.5-pro.\n"
             "• Frontier open-source: qwen2.5-vl:72b (Alibaba — among the strongest open vision "
             "models), glm-4v-plus, internvl2:26b, llava-1.6:34b, pixtral-12b.\n"
             "• Cheap & fast: claude-haiku-4-5, gpt-4o-mini, gemini-1.5-flash, qwen2-vl:7b."},
    {"key": "vision_llm_endpoint", "section": "Vision model",
     "label": "Endpoint URL (only when wire protocol = openai_compat)", "type": "text",
     "help": "Same endpoint conventions as the Translation model. The two slots are "
             "INDEPENDENT — common pattern: cloud LLM for translation + local Ollama running "
             "qwen2.5-vl for vision (vision is the slot that benefits most from a strong "
             "specialized model)."},
    {"key": "vision_llm_api_key", "section": "Vision model",
     "label": "API key (LEAVE BLANK for local servers)", "type": "password",
     "help": "REQUIRED for cloud providers. LEAVE BLANK for default local Ollama / LM Studio / "
             "LocalAI. Independent from the Translation slot — paste the same value in both "
             "if you're using one provider for everything."},

    # ── Scene & Cinematic tuning (no effect when mode=audio) ──────────────────
    {"key": "scene_detection_threshold", "section": "Scene & Cinematic",
     "label": "Scene-detection threshold", "type": "number",
     "help": "ffmpeg's scene-change threshold, 0.0–1.0. Lower → more scenes detected. "
             "0.3–0.5 is typical for film/TV; lower for fast-cut content."},
    {"key": "scene_min_length_seconds", "section": "Scene & Cinematic",
     "label": "Min scene length (seconds)", "type": "number",
     "help": "Skip scenes shorter than this — avoids micro-shots polluting the bible."},
    {"key": "scene_max_scenes", "section": "Scene & Cinematic",
     "label": "Max scenes per file (hard cap)", "type": "number",
     "help": "~200 typical for a 2-hour film. Higher = more vision-LLM calls = more $/wait."},
    {"key": "scene_keyframe_position", "section": "Scene & Cinematic",
     "label": "Keyframe sample position", "type": "select",
     "options": [
         {"value": "midpoint", "label": "midpoint · [safest] center of the shot · default"},
         {"value": "start",    "label": "start · first frame of the shot"},
         {"value": "end",      "label": "end · last frame of the shot"},
     ],
     "help": "Where in each scene to grab the representative frame for the vision LLM."},
    {"key": "scene_frame_max_size", "section": "Scene & Cinematic",
     "label": "Scene keyframe max long edge (px)", "type": "number",
     "help": "Resolution sent to the Vision LLM for the scene bible. Smaller = cheaper "
             "+ faster, but loses fine details (small on-screen text gets unreadable below ~600px)."},
    {"key": "scene_bible_batch_size", "section": "Scene & Cinematic",
     "label": "Scenes per bible-build call", "type": "number",
     "help": "How many keyframes the Vision LLM describes per API call. 10 is a good balance."},
    {"key": "cinematic_frame_max_size", "section": "Scene & Cinematic",
     "label": "Cinematic per-cue frame max long edge (px)", "type": "number",
     "help": "Smaller default than scene keyframes since cinematic ships one frame "
             "per cue (potentially 1000+ images per film). Shrinking saves a lot."},
    {"key": "cinematic_batch_size", "section": "Scene & Cinematic",
     "label": "Cues per cinematic call", "type": "number",
     "help": "Smaller than the text-only batch (default 30) because each call ships "
             "one image per cue. 10 keeps each call manageable."},

    # ── Subtitle formatting ───────────────────────────────────────────────────
    {"key": "max_line_chars", "section": "Subtitles",
     "label": "Max chars per line", "type": "number",
     "help": "Standard subtitling guidelines suggest 40–42 for comfortable reading."},
    {"key": "max_lines_per_cue", "section": "Subtitles",
     "label": "Max lines per cue", "type": "number",
     "help": "Overflow merges into the last line — never drops content."},
    {"key": "polish_enabled", "section": "Subtitles",
     "label": "Readability polish (extend short cues, merge fragments)",
     "type": "checkbox",
     "help": "Whisper outputs tight per-utterance timing — a 0.3 s "
             "'Yes.' gets a 0.3 s cue, far too brief to read. With "
             "this ON (default), a final pass extends cues to a "
             "minimum display duration (capped to never overlap the "
             "next cue) and optionally merges adjacent fragments "
             "that visually read as one subtitle. Inception "
             "comparison: raw Whisper had 42.8 % of cues under 1 s, "
             "the pro reference SRT had 0. Defaults match the pro "
             "shape closely. Turn OFF only if you want the raw "
             "Whisper timing (e.g. for downstream tooling that "
             "does its own readability pass)."},
    {"key": "min_cue_duration_seconds", "section": "Subtitles",
     "label": "Min display duration (s)", "type": "number",
     "show_if": {"field": "polish_enabled", "equals": "true"},
     "help": "Lower bound on how long any cue stays on screen, "
             "regardless of utterance length. 1.2 s is the lower "
             "end of pro subtitling norms; BBC guidelines allow "
             "0.7-1.5 s, Netflix expects 5/6 s for single syllables. "
             "Shorter cues are extended forward (never the start — "
             "audio onset stays in sync)."},
    {"key": "min_seconds_per_char", "section": "Subtitles",
     "label": "Min reading speed (seconds per character)",
     "type": "number",
     "show_if": {"field": "polish_enabled", "equals": "true"},
     "help": "Reading-speed cap. 0.045 s/char ≈ 22 chars/second, a "
             "relaxed pace. A 40-character two-line cue claims at "
             "least 40 × 0.045 = 1.8 s. Tighten (smaller value) for "
             "faster readers, loosen for slower."},
    {"key": "merge_adjacent_cues", "section": "Subtitles",
     "label": "Merge adjacent short cues", "type": "checkbox",
     "show_if": {"field": "polish_enabled", "equals": "true"},
     "help": "When two consecutive cues are visually one subtitle "
             "(small gap between them, combined text fits in the "
             "line-wrap budget), collapse them into one. Trims the "
             "'flickery sequence of short fragments' effect Whisper "
             "produces on quick back-and-forth dialog."},
    {"key": "max_gap_to_merge_seconds", "section": "Subtitles",
     "label": "Max gap to merge (s)", "type": "number",
     "show_if": {"field": "merge_adjacent_cues", "equals": "true"},
     "help": "Two cues are merge candidates only when the silence "
             "between them is shorter than this. 0.3 s = a natural "
             "breath in conversation. Above this they're treated as "
             "separate utterances."},
    {"key": "max_merged_cue_duration_seconds", "section": "Subtitles",
     "label": "Max merged cue duration (s)", "type": "number",
     "show_if": {"field": "merge_adjacent_cues", "equals": "true"},
     "help": "Hard cap on how long a single merged cue can be on "
             "screen. Beyond this, a subtitle reads as cluttered "
             "rather than coherent. 7 s is the standard upper bound."},
    {"key": "cue_separation_seconds", "section": "Subtitles",
     "label": "Min gap between cues (s)", "type": "number",
     "show_if": {"field": "polish_enabled", "equals": "true"},
     "help": "Minimum silence kept between consecutive cues after "
             "the polish pass extends durations. 0.05 s ≈ 1 frame "
             "at 24 fps — invisible to the eye but prevents two "
             "subtitles overlapping in renderers that handle the "
             "overlap case clumsily."},

    # ── Resource safety (advanced — sits at the bottom of the form) ─────────
    # These caps prevent a long film + heavy mode from consuming all host
    # RAM. They complement the cgroup limits in docker-compose.yml.
    {"key": "job_timeout_seconds", "section": "Resource safety",
     "label": "Job wall-clock timeout (seconds)", "type": "number",
     "help": "Hard cap on a single job's runtime. 5400 = 90 min — generous for "
             "a 3 h film at whisper-large on int8 CPU. Set to 0 to disable. "
             "Enforced at every pipeline checkpoint (between audio segments, "
             "between translation batches, between scene-detect ffmpeg lines) "
             "so a wedged job can't hold the queue indefinitely."},
    {"key": "stt_audio_segment_seconds", "section": "Resource safety",
     "label": "OpenVINO STT audio-segment size (seconds)", "type": "number",
     "help": "How much audio is loaded into RAM at once for the OpenVINO "
             "Whisper backend. 600 = 10 min, ~75 MB resident regardless of "
             "film length. Lower values reduce RAM further; higher values "
             "have fewer segment-boundary cue splits. Ignored when "
             "whisper_backend = cpu (faster-whisper streams from disk on its own)."},
    {"key": "cinematic_max_cues_with_frames", "section": "Resource safety",
     "label": "Cinematic — max cues that get a frame attached", "type": "number",
     "help": "Hard cap on per-cue frame extraction in cinematic mode. A 2 h+ "
             "dialog-heavy film can produce 1500+ cues — pre-extracting one "
             "JPEG per cue is what caused the original TrueNAS OOM. With this "
             "cap, only the first N cues ship frames; remaining cues translate "
             "text-only (still using the scene bible). Set to 0 to disable "
             "per-cue frames entirely (cinematic ≈ scene mode)."},

    # ── Security ────────────────────────────────────────────────────────────
    {"key": "auth_credentials", "section": "Security",
     "label": "HTTP Basic credentials (user:password)", "type": "password",
     "help": "Leave BLANK for no auth (default — preserves zero-config first "
             "boot). Set to 'user:password' to require Basic auth on every "
             "endpoint except /health. Adds a same-origin check on POST/PATCH/"
             "PUT/DELETE so a malicious LAN page can't ride your saved browser "
             "credentials to start jobs. Apply this on any network where you "
             "wouldn't trust every device — the Library page can queue jobs "
             "that consume your LLM quota."},
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
        "default_target_lang", "default_mode", "default_translation_provider",
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
    mode: str | None = None,
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
                "mode": mode or settings.default_mode,
                "q": q or "",
                "missing_only": bool(missing_only),
                "start_index": 0, "limit": limit,
                "modes": list(SUPPORTED_MODES),
                "language_options": LANGUAGE_OPTIONS,
                "libraries": [],
                "library_id": library_id or "",
                "error": None,
            },
        )

    target_lang = target_lang or settings.default_target_lang
    mode = mode or settings.default_mode

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
            "mode": mode,
            "q": q or "",
            "missing_only": bool(missing_only),
            "start_index": start_index,
            "limit": limit,
            "modes": list(SUPPORTED_MODES),
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
    # ``Inception.fr.audio.ai.vtt`` → ``Inception`` after stripping
    # ``.vtt`` → ``.ai`` → ``.<mode>`` → ``.<lang>``. We don't enforce
    # a known mode/lang set here because the search is just a
    # heuristic best-effort; a wrong basename simply produces no
    # match and the function falls back to None.
    base = out_name
    if base.endswith(".vtt"):
        base = base[:-4]
    if base.endswith(".ai"):
        base = base[:-3]
    for mode in (".audio", ".scene", ".cinematic"):
        if base.endswith(mode):
            base = base[: -len(mode)]
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
    return templates.TemplateResponse(
        request,
        "cache_stats.html",
        {
            "stats": record,
            "cache_key": cache_key,
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
    if settings.vocal_isolation_enabled:
        from app.pipeline import vocal_isolation as vi
        ok, err = vi.is_available()
        if not ok:
            warnings["vocal_isolation_enabled"] = (
                "demucs is NOT installed in this image. Any job "
                "submitted while this is ON will fail fast at "
                "queue time. Fix: rebuild the image with "
                "`git pull && docker compose build && "
                "docker compose up -d` (the shipped Dockerfiles "
                "include demucs as of 0.7.23), or turn this OFF."
            )
    return warnings


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
            "active": "settings",
            "saved": error is None,
            "error": error,
        },
    )
