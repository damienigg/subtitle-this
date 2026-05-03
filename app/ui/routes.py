"""HTML routes for the web UI. Server-rendered Jinja2 + HTMX for interactivity.

Only HTML lives here; data routes live in app/api/*. The settings form posts
to /api/settings (PATCH) via HTMX, then re-renders the whole settings panel.
"""
from typing import Any, get_type_hints

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import jobs
from app.api.manage import emby_client
from app.config import READ_ONLY_FIELDS, SENSITIVE_FIELDS, _EnvSettings, settings
from app.emby.client import EmbyError
from app.processor import SUPPORTED_MODES


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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
    "Speech-to-Text": (
        "Whisper transcribes audio to text. ALWAYS FREE — runs 100% locally, "
        "model is downloaded once. The trade-off here is compute time × quality "
        "× disk space, NOT money."
    ),
    "Defaults": (
        "Pre-set choices applied when you click 'Subtitle this' or hit "
        "/api/process without query overrides. THIS is where the cost/complexity "
        "lever lives — Provider × Mode determines whether each job is free, "
        "cheap, or expensive."
    ),
    "Translation": (
        "Provider-agnostic translation knobs. Most users leave these at defaults."
    ),
    "Translation model": (
        "Only used when Translation provider = LLM. Skip this section entirely "
        "if you stay on NLLB or DeepL. Configure cloud (Anthropic / OpenAI / "
        "OpenRouter / …) or fully local (Ollama / LM Studio / LocalAI / vLLM) "
        "— Babel doesn't care which."
    ),
    "Vision model": (
        "Only used by scene and cinematic modes (the LLM that describes "
        "keyframes for the scene bible). Skip this section entirely if you "
        "stay on audio mode."
    ),
    "Scene & Cinematic": (
        "Tuning knobs for the multimodal modes. NO EFFECT when mode = audio. "
        "Most users can leave these at defaults."
    ),
    "Subtitles": (
        "WebVTT line-wrap formatting."
    ),
    "Emby": (
        "How Babel Tower reaches your Emby server."
    ),
    "API keys": (
        "Cloud provider keys. Leave everything blank for fully-local setups."
    ),
}


_FIELD_META: list[dict[str, Any]] = [
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
         "• openvino exports Whisper to OpenVINO IR and runs the encoder on the Intel iGPU. "
         "Only works in the openvino-flavored image with /dev/dri exposed (TrueNAS Scale "
         "with N305/N100/iGPU-equipped Intel host)."
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
     "label": "Compute type (CPU backend only)", "type": "select",
     "options": [
         {"value": "int8",    "label": "int8 · [fastest · lowest precision] default — works well in practice"},
         {"value": "int16",   "label": "int16 · [fast · slightly more precise]"},
         {"value": "float16", "label": "float16 · [slow · high precision]"},
         {"value": "float32", "label": "float32 · [slowest · full precision · rarely worth it]"},
     ],
     "help": "Quantization for faster-whisper. Lower precision = faster + less RAM, "
             "with negligible quality loss for subtitle work."},
    {"key": "whisper_device", "section": "Speech-to-Text",
     "label": "Device (CPU backend only)", "type": "select",
     "options": [
         {"value": "cpu",  "label": "cpu · [universal] works on any host"},
         {"value": "cuda", "label": "cuda · [NVIDIA GPU] needs nvidia-container-toolkit (rare on TrueNAS)"},
     ],
     "help": "Where faster-whisper runs. cuda only matters if you've added an NVIDIA GPU."},
    {"key": "openvino_device", "section": "Speech-to-Text",
     "label": "OpenVINO device (OpenVINO backend only)", "type": "select",
     "options": [
         {"value": "GPU",  "label": "GPU · [iGPU · fastest] default for openvino backend"},
         {"value": "CPU",  "label": "CPU · [no special hardware · slower] OpenVINO running on the CPU"},
         {"value": "AUTO", "label": "AUTO · let OpenVINO pick"},
     ]},

    # ── Defaults — the cost lever ─────────────────────────────────────────────
    {"key": "default_target_lang", "section": "Defaults",
     "label": "Default target language", "type": "text",
     "help": "ISO 639-1 code: en, fr, ja, es, de, ja, ja, zh, ar, …"},
    {"key": "default_source_lang_priority", "section": "Defaults",
     "label": "Source language priority", "type": "text",
     "help": "Comma-separated. '*' is a wildcard that matches any language. "
             "Order = preference: 'en,ja,*' picks English audio first, then Japanese, "
             "then anything else."},
    {"key": "default_translation_provider", "section": "Defaults",
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
         "European and East-Asian pairs. Requires a DeepL API key in the API keys section.\n"
         "• LLM — uses whatever you configure in the Translation model section. Highest "
         "quality, supports any language pair. Free if you point at local Ollama / LM Studio. "
         "Paid per-token if you point at Anthropic / OpenAI / OpenRouter / etc. The only "
         "provider that benefits from scene/cinematic visual context."
     )},
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
     "help": "When a film's audio track has no language tag (Emby just shows 'Audio') we "
             "always run a Whisper-tiny pre-pass to detect the language for the transcription "
             "itself. With this checkbox ON, we ALSO write that language back into the file's "
             "EBML header via `mkvpropedit` — instant, modifies only metadata, NEVER touches "
             "the audio/video data sections. Restricted to MKV/MKA/WebM. Non-Matroska "
             "containers (MP4/MOV/AVI/...) are deliberately left untouched: an ffmpeg remux "
             "would technically preserve audio byte-for-byte but rewrites the whole file with "
             "documented edge cases (timestamp re-derivation, lost custom metadata) — not "
             "worth the risk on a media library. Detection still drives transcription "
             "correctness regardless of container; only the persist-to-Emby step is skipped "
             "for non-MKV. Turn off entirely to keep all source files completely pristine."},

    # ── Translation (provider-agnostic params) ────────────────────────────────
    {"key": "nllb_model", "section": "Translation",
     "label": "NLLB HF model id (only used when provider=nllb)", "type": "text",
     "help": "facebook/nllb-200-distilled-600M (default, ~1.5 GB) is the sweet spot. "
             "Larger variants exist (1.3B, 3.3B) — better quality, slower, more RAM."},
    {"key": "translation_batch_size", "section": "Translation",
     "label": "Cues per LLM batch (text-only mode, only used when provider=llm)", "type": "number",
     "help": "Higher = fewer round-trips, lower = more granular failures and retries. "
             "30 is a good balance."},

    # ── Translation model (only used when provider=llm) ───────────────────────
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

    # ── Emby integration ──────────────────────────────────────────────────────
    {"key": "emby_url", "section": "Emby",
     "label": "Emby server URL", "type": "text",
     "help": "Where Babel reaches Emby. e.g. http://emby:8096 (docker-compose service name) "
             "or http://192.168.1.10:8096 (LAN IP)."},
    {"key": "emby_api_key", "section": "Emby",
     "label": "Emby API key", "type": "password",
     "help": "Generate in Emby admin → Server Settings → Advanced → API Keys."},

    # ── API keys ──────────────────────────────────────────────────────────────
    {"key": "deepl_api_key", "section": "API keys",
     "label": "DeepL API key", "type": "password",
     "help": "Required when Translation provider = DeepL. Free-tier keys end in ':fx' "
             "(auto-detected — Babel routes to api-free.deepl.com vs api.deepl.com)."},
]


def _section_groups() -> list[tuple[str, str, list[dict]]]:
    """Group fields by section. Returns (name, description, fields) tuples in
    display order."""
    seen: list[str] = []
    for f in _FIELD_META:
        if f["section"] not in seen:
            seen.append(f["section"])
    return [
        (s, _SECTION_META.get(s, ""), [f for f in _FIELD_META if f["section"] == s])
        for s in seen
    ]


def _coerce(key: str, raw: str) -> Any:
    """Coerce a form-submitted string to the type pydantic expects on the env model."""
    hints = get_type_hints(_EnvSettings)
    target = hints.get(key, str)
    target_str = str(target)

    if target is bool or "bool" in target_str:
        return raw in ("on", "true", "True", "1", "yes")
    if target is int or "int" in target_str:
        return int(raw) if raw != "" else 0
    if target is float or "float" in target_str:
        return float(raw) if raw != "" else 0.0
    if "list" in target_str:
        return [s.strip() for s in raw.split(",") if s.strip()]
    return raw


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "jobs": jobs.list_jobs(20),
            "emby_configured": bool(settings.emby_url and settings.emby_api_key),
            "settings": settings.all_values(mask_sensitive=True),
            "active": "dashboard",
        },
    )


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
) -> HTMLResponse:
    """Browse Emby items, filter, and queue per-item subtitling jobs."""
    if not settings.emby_url or not settings.emby_api_key:
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
                "error": None,
            },
        )

    target_lang = target_lang or settings.default_target_lang
    mode = mode or settings.default_mode

    error = None
    items: list[dict] = []
    total = 0
    try:
        page = emby_client().list_videos(start_index=start_index, limit=limit, search_term=q or None)
        for it in page.items:
            has_sub = it.has_subtitle_track(target_lang)
            if missing_only and has_sub:
                continue
            items.append({
                "id": it.id, "name": it.name, "type": it.type,
                "path": it.path, "has_target_subtitle": has_sub,
            })
        total = page.total
    except (EmbyError, HTTPException) as e:
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
            "error": error,
        },
    )


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
            "active": "settings",
            "saved": error is None,
            "error": error,
        },
    )
