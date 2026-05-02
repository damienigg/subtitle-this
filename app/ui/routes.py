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


# Field metadata drives the settings form rendering. Order here = display order.
_FIELD_META: list[dict[str, Any]] = [
    # STT
    {"key": "whisper_backend", "section": "Speech-to-Text",
     "label": "Backend", "type": "select",
     "options": [
         {"value": "cpu",
          "label": "cpu — faster-whisper (works on any host, slower without GPU)"},
         {"value": "openvino",
          "label": "openvino — Intel iGPU via optimum-intel (TrueNAS N305, 5–10× faster)"},
     ],
     "help": (
         "• cpu uses faster-whisper, runs entirely on the CPU. INT8 quantization keeps it "
         "tractable but a 2-hour film on a small/medium model takes 20–60 minutes.\n"
         "• openvino exports Whisper to OpenVINO IR and runs the encoder on Intel iGPU. "
         "Only works in the openvino-flavored image with /dev/dri exposed (TrueNAS Scale "
         "with an N305 / N100 / iGPU-equipped Intel host)."
     )},
    {"key": "whisper_model", "section": "Speech-to-Text", "label": "Whisper model", "type": "select",
     "options": ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]},
    {"key": "whisper_compute_type", "section": "Speech-to-Text", "label": "Compute type (CPU backend)",
     "type": "select", "options": ["int8", "int16", "float16", "float32"]},
    {"key": "whisper_device", "section": "Speech-to-Text", "label": "Device (CPU backend)",
     "type": "select", "options": ["cpu", "cuda"],
     "help": "Where faster-whisper runs. cuda only works if you've added an NVIDIA "
             "GPU + nvidia-container-toolkit to the host."},
    {"key": "openvino_device", "section": "Speech-to-Text", "label": "OpenVINO device",
     "type": "select", "options": ["GPU", "CPU", "AUTO"]},

    # ── Translation model ─────────────────────────────────────────────────
    # Translates subtitle cues. In cinematic mode this same model also receives
    # per-cue keyframes — pick a vision-capable one and toggle the flag if you
    # plan to use cinematic mode.
    {"key": "translation_llm_type", "section": "Translation model",
     "label": "Wire protocol", "type": "select",
     "options": ["anthropic", "openai_compat"],
     "help": "`anthropic` ONLY for native Claude (prompt caching, adaptive thinking, strict "
             "JSON-schema). For EVERYTHING ELSE — including OpenAI, all local servers (Ollama, "
             "LM Studio, LocalAI, vLLM, llama.cpp), and any other cloud (OpenRouter, Together, "
             "Groq, DeepSeek, Zhipu, Gemini-compat) — pick `openai_compat` and set the endpoint."},
    {"key": "translation_llm_model", "section": "Translation model",
     "label": "Model", "type": "text",
     "help": "What makes a good translator: large parameter count, broad multilingual "
             "training, strong instruction-following. Cloud frontier: claude-opus-4-7, "
             "claude-sonnet-4-6, gpt-4o, gpt-4o-mini, gemini-1.5-pro, gemini-2.0-flash-exp, "
             "mistral-large-latest. Open-source — chinese-strong: qwen2.5:72b, "
             "qwen2.5:32b, deepseek-v3, deepseek-chat, glm-4-flash. Open-source — "
             "general: llama3.1:70b, llama3.3:70b, mistral-large, command-r-plus. "
             "Smaller-but-capable: claude-haiku-4-5, gpt-4o-mini, qwen2.5:14b, "
             "llama3.1:8b, gemma2:27b."},
    {"key": "translation_llm_endpoint", "section": "Translation model",
     "label": "Endpoint URL (when wire protocol = openai_compat)", "type": "text",
     "help": "Ignored when wire protocol = anthropic. Examples — CLOUD: "
             "https://api.openai.com/v1 (OpenAI) · "
             "https://openrouter.ai/api/v1 (OpenRouter — many models behind one URL) · "
             "https://api.deepseek.com/v1 (DeepSeek native) · "
             "https://open.bigmodel.cn/api/paas/v4 (Zhipu / GLM) · "
             "https://generativelanguage.googleapis.com/v1beta/openai (Gemini compat). "
             "LOCAL (no API key needed): http://ollama:11434/v1 · "
             "http://lmstudio:1234/v1 · http://localai:8080/v1 · "
             "http://host.docker.internal:1234/v1 (LM Studio on the host machine when Babel runs in Docker)."},
    {"key": "translation_llm_api_key", "section": "Translation model",
     "label": "API key (optional for local servers)", "type": "password",
     "help": "REQUIRED for cloud providers (Anthropic, OpenAI, OpenRouter, Together, Groq, "
             "DeepSeek, Zhipu, Gemini, ...). LEAVE BLANK for local servers (Ollama, LM Studio, "
             "LocalAI) that don't authenticate by default — Babel substitutes a placeholder so "
             "the OpenAI SDK is happy. Set a value only if you've explicitly enabled auth on "
             "your local server (e.g. vLLM with --api-key)."},
    {"key": "translation_llm_supports_vision", "section": "Translation model",
     "label": "Supports vision (required for cinematic mode)", "type": "checkbox",
     "help": "Whether this model accepts image inputs. Cinematic mode attaches one frame "
             "per cue to translation calls — needs a multimodal model (claude-opus-4-7, "
             "gpt-4o, gemini-1.5-pro, qwen2.5-vl, llava, etc.). Anthropic models always "
             "support vision (flag is ignored when type=anthropic)."},

    # ── Vision model ──────────────────────────────────────────────────────
    # Builds the scene bible: 1-2 sentence description per shot. Used by scene
    # and cinematic modes. By definition has to be vision-capable.
    {"key": "vision_llm_enabled", "section": "Vision model",
     "label": "Enable scene/cinematic modes", "type": "checkbox",
     "help": "Master switch. Toggle off if you don't have a vision-capable LLM and only "
             "use audio mode. When off, scene/cinematic modes 400 immediately."},
    {"key": "vision_llm_type", "section": "Vision model",
     "label": "Wire protocol", "type": "select",
     "options": ["anthropic", "openai_compat"],
     "help": "Same as the Translation model: `anthropic` for native Claude, `openai_compat` "
             "for everything else (cloud or local)."},
    {"key": "vision_llm_model", "section": "Vision model",
     "label": "Model", "type": "text",
     "help": "What makes a good vision describer: strong OCR (read on-screen text), "
             "scene-understanding (count/identify characters, recognize settings), and "
             "concise output. Cloud frontier: claude-opus-4-7, claude-sonnet-4-6, gpt-4o, "
             "gemini-1.5-pro, gemini-2.0-flash-exp. Open-source — chinese-strong: "
             "qwen2.5-vl:72b, qwen2.5-vl:7b (Alibaba; among the strongest open-source "
             "vision models), glm-4v-plus, internvl2:26b (Shanghai AI Lab). "
             "Open-source — general: llava:34b, llava:13b, llava-1.6:34b, "
             "minicpm-v:8b, pixtral-12b. Cheap-and-fast: claude-haiku-4-5, gpt-4o-mini, "
             "gemini-1.5-flash, qwen2-vl:7b."},
    {"key": "vision_llm_endpoint", "section": "Vision model",
     "label": "Endpoint URL (when wire protocol = openai_compat)", "type": "text",
     "help": "Same endpoint conventions as the translation model. The two slots are "
             "independent — common pattern: OpenAI for translation, Ollama running "
             "qwen2.5-vl locally for vision."},
    {"key": "vision_llm_api_key", "section": "Vision model",
     "label": "API key (optional for local servers)", "type": "password",
     "help": "REQUIRED for cloud providers, LEAVE BLANK for default local Ollama / LM Studio / "
             "LocalAI installs. Independent from the translation slot — paste the same value in "
             "both if you're using one provider for everything."},

    # Translation
    {"key": "nllb_model", "section": "Translation", "label": "NLLB HF model id", "type": "text"},
    {"key": "translation_batch_size", "section": "Translation",
     "label": "Cues per LLM batch (text-only)", "type": "number"},

    # Subtitle formatting
    {"key": "max_line_chars", "section": "Subtitles", "label": "Max chars per line", "type": "number"},
    {"key": "max_lines_per_cue", "section": "Subtitles", "label": "Max lines per cue", "type": "number"},

    # Defaults applied to UI- and webhook-triggered jobs
    {"key": "default_target_lang", "section": "Defaults", "label": "Default target language",
     "type": "text", "help": "ISO 639-1 (en, fr, ja, ...)"},
    {"key": "default_source_lang_priority", "section": "Defaults",
     "label": "Source language priority", "type": "text",
     "help": "comma-separated; '*' is a wildcard that matches any language"},
    {"key": "default_translation_provider", "section": "Defaults",
     "label": "Who translates the cues?", "type": "select",
     "options": [
         {"value": "llm",
          "label": "LLM — use the Translation model configured above (recommended)"},
         {"value": "deepl",
          "label": "DeepL — cloud translation API (set DeepL API key below)"},
         {"value": "nllb",
          "label": "NLLB-200 — local 200-language model (OpenVINO image only)"},
     ],
     "help": (
         "Each option requires different setup:\n"
         "• LLM uses whatever you configured in the Translation model section above "
         "(Claude / GPT / Llama / Qwen / DeepSeek / Gemini / ...). Best quality. Vision-aware "
         "in scene/cinematic modes.\n"
         "• DeepL is a dedicated translation API. Free tier: 500k characters/month "
         "(~6 movies). Excellent for European languages. Text-only — never sees the picture. "
         "Requires the DeepL API key in the API keys section below.\n"
         "• NLLB-200 is Meta's 200-language model. Runs locally on the Intel iGPU via "
         "OpenVINO. Free, offline. Lower quality than the cloud options on common pairs but "
         "covers the long tail. Only available in the openvino-flavored Docker image."
     )},
    {"key": "default_mode", "section": "Defaults",
     "label": "Quality tier (and what visual context to add)", "type": "select",
     "options": [
         {"value": "audio",
          "label": "audio — speech only · fastest · cheapest"},
         {"value": "scene",
          "label": "scene — + LLM-vision scene bible (pronoun & gender disambiguation)"},
         {"value": "cinematic",
          "label": "cinematic — scene + per-cue keyframe attached to translation"},
     ],
     "help": (
         "• audio uses Whisper transcription only — no visual context. Always works.\n"
         "• scene runs ffmpeg scene-detection on the video, sends one keyframe per shot to "
         "the Vision model for a 1-2 sentence description, then feeds the resulting bible to "
         "the translator as cached system context. Requires the Vision model section enabled.\n"
         "• cinematic does what scene does AND additionally attaches one keyframe per cue to "
         "the translation call so the translator literally sees the moment for each line. "
         "Requires the Translation model to be vision-capable."
     )},
    {"key": "default_skip_if_target_audio_exists", "section": "Defaults",
     "label": "Skip when target-language audio is already present", "type": "checkbox"},

    # Scene detection (used by scene + cinematic modes)
    {"key": "scene_detection_threshold", "section": "Scene & Cinematic",
     "label": "Scene-detection threshold", "type": "number",
     "help": "ffmpeg's scene-change threshold, 0.0–1.0. Lower → more scenes detected. "
             "0.3–0.5 typical for film/TV; lower for fast-cut content."},
    {"key": "scene_min_length_seconds", "section": "Scene & Cinematic",
     "label": "Min scene length (s)", "type": "number",
     "help": "Skip scenes shorter than this — avoids micro-shots polluting the bible."},
    {"key": "scene_max_scenes", "section": "Scene & Cinematic",
     "label": "Max scenes per file", "type": "number",
     "help": "Hard cap. ~200 typical for a 2h film. Higher costs more on first build."},
    {"key": "scene_keyframe_position", "section": "Scene & Cinematic",
     "label": "Keyframe sample position", "type": "select",
     "options": ["start", "midpoint", "end"],
     "help": "Where in each scene to grab the representative frame. Midpoint is safest."},
    {"key": "scene_frame_max_size", "section": "Scene & Cinematic",
     "label": "Scene keyframe max long edge (px)", "type": "number",
     "help": "Resolution sent to Claude vision for the scene bible. Smaller = cheaper."},
    {"key": "scene_bible_batch_size", "section": "Scene & Cinematic",
     "label": "Scenes per bible-build call", "type": "number",
     "help": "How many keyframes Claude describes per API call. 10 is a good balance."},
    {"key": "cinematic_frame_max_size", "section": "Scene & Cinematic",
     "label": "Cinematic per-cue frame max long edge (px)", "type": "number",
     "help": "Smaller default than scene keyframes since cinematic ships one frame "
             "per cue (potentially 1000+ images per film). Smaller saves a lot."},
    {"key": "cinematic_batch_size", "section": "Scene & Cinematic",
     "label": "Cues per cinematic call", "type": "number",
     "help": "Smaller than the text-only batch (default 30) because each call "
             "ships one image per cue. 10 keeps each call manageable."},

    # Emby
    {"key": "emby_url", "section": "Emby", "label": "Emby server URL", "type": "text",
     "help": "e.g. http://emby:8096"},
    {"key": "emby_api_key", "section": "Emby", "label": "Emby API key", "type": "password"},
    {"key": "webhook_secret", "section": "Emby", "label": "Webhook shared secret",
     "type": "password",
     "help": "Optional. If set, /webhook/emby requires header X-Babel-Token: <this value>."},

    # API keys
    {"key": "deepl_api_key", "section": "API keys",
     "label": "DeepL API key", "type": "password",
     "help": "Required when default_translation_provider=deepl. Free-tier keys end in ':fx' (auto-detected)."},
]


def _section_groups() -> list[tuple[str, list[dict]]]:
    seen: list[str] = []
    for f in _FIELD_META:
        if f["section"] not in seen:
            seen.append(f["section"])
    return [(s, [f for f in _FIELD_META if f["section"] == s]) for s in seen]


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
