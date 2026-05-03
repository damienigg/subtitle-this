"""Core pipeline orchestrator. The Emby-driven job runner (queued via the
UI's per-item "Subtitle this" button or the dashboard's "Sweep library"
button) calls into here. Subtitle creation is exclusively a manual user
action — there is no auto-trigger or path-based CLI flow.

Modes:
- `audio` — Whisper → text translator. No vision.
- `scene` — adds an LLM-vision scene bible: detect shots, extract one
  keyframe per shot, ask the configured Vision LLM for a 1-2 sentence
  description of each, then send the whole bible as cached system context for
  the translation calls.
- `cinematic` — everything `scene` does, plus a per-cue keyframe attached as
  an image block to the translation call so the translator literally sees what
  is on screen for each line.

Errors are raised as typed subclasses of ProcessError so callers can map them
to specific HTTP status codes without resorting to string matching.
"""
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app import cache as cache_mod
from app.config import settings
from app.pipeline import audio, frames, scene_bible, scenes, stt, tracks
from app.pipeline.translate import TranslationError, get_provider
from app.pipeline.translate.base import SceneInfo, TranslationContext
from app.pipeline.vtt import to_webvtt


SUPPORTED_MODES = ("audio", "scene", "cinematic")
MULTIMODAL_MODES = ("scene", "cinematic")


class ProcessError(Exception):
    """Base for all pipeline errors. Subclasses map cleanly to HTTP status codes."""


class MediaNotFound(ProcessError):
    pass


class NoSpeech(ProcessError):
    pass


class NoSuitableTrack(ProcessError):
    pass


class BadRequest(ProcessError):
    pass


class TranslationFailed(ProcessError):
    pass


@dataclass
class ProcessRequest:
    media_path: str
    target_lang: str
    source_lang_priority: list[str]
    translation_provider: str
    mode: str = "audio"
    skip_if_target_audio_exists: bool = True


@dataclass
class ProcessResult:
    vtt: str
    source_track_index: int
    source_track_language: str | None
    source_track_title: str | None
    detected_source_language: str
    cue_count: int
    mode: str
    cached: bool
    took_seconds: float


def process(req: ProcessRequest) -> ProcessResult:
    started = time.monotonic()

    if req.mode not in SUPPORTED_MODES:
        raise BadRequest(f"unknown mode {req.mode!r} (expected one of {SUPPORTED_MODES})")
    if req.mode in MULTIMODAL_MODES:
        if req.translation_provider not in ("llm", "claude"):
            raise BadRequest(
                f"mode={req.mode!r} requires translation_provider='llm' "
                "(only the LLM provider talks to a multimodal backend)."
            )
        if not settings.vision_llm_enabled:
            raise BadRequest(
                f"mode={req.mode!r} requires the Vision LLM to be enabled. Configure a "
                "vision-capable model in Settings → Vision model and toggle vision_llm_enabled."
            )
        # Validate the configured vision LLM has its credentials.
        v_type = (settings.vision_llm_type or "").lower()
        if v_type == "anthropic":
            if not settings.vision_llm_api_key:
                raise BadRequest(
                    f"mode={req.mode!r} with vision_llm_type=anthropic requires "
                    "vision_llm_api_key to be set."
                )
        elif v_type == "openai_compat":
            if not settings.vision_llm_endpoint:
                raise BadRequest(
                    f"mode={req.mode!r} with vision_llm_type=openai_compat requires "
                    "vision_llm_endpoint to be set."
                )
        # Cinematic also attaches frames to the translation LLM, so that one
        # also has to be vision-capable.
        if req.mode == "cinematic" and not settings.translation_llm_supports_vision:
            raise BadRequest(
                "cinematic mode also attaches per-cue frames to the translation LLM. "
                "Enable translation_llm_supports_vision in Settings, pick a vision-capable "
                "translation model, or use scene mode instead."
            )

    media = Path(req.media_path)
    if not media.exists():
        raise MediaNotFound(f"media not found: {req.media_path}")

    fp = cache_mod.file_fingerprint(media)
    # The cache key is built from every input that affects output:
    # - For scene/cinematic, the detection threshold + the vision LLM model
    #   (the bible depends on it).
    # - For provider=llm, the translation LLM model (output depends on it
    #   directly). DeepL/NLLB don't have a model setting that varies, so we
    #   pass None and they fall through to a single key per provider.
    threshold = settings.scene_detection_threshold if req.mode in MULTIMODAL_MODES else None
    tllm_model = (
        settings.translation_llm_model if req.translation_provider == "llm" else None
    )
    vllm_model = (
        settings.vision_llm_model if req.mode in MULTIMODAL_MODES else None
    )
    key = cache_mod.cache_key(
        fp,
        req.target_lang,
        settings.whisper_model,
        req.translation_provider,
        req.source_lang_priority,
        req.mode,
        scene_threshold=threshold,
        translation_llm_model=tllm_model,
        vision_llm_model=vllm_model,
    )
    cached = cache_mod.load(key)
    if cached:
        return ProcessResult(
            vtt=cached["vtt"],
            source_track_index=cached["source_track"]["index"],
            source_track_language=cached["source_track"]["language"],
            source_track_title=cached["source_track"]["title"],
            detected_source_language=cached["detected_source_language"],
            cue_count=cached["cue_count"],
            mode=cached.get("mode", req.mode),
            cached=True,
            took_seconds=time.monotonic() - started,
        )

    audio_tracks = tracks.probe(req.media_path)
    if not audio_tracks:
        raise NoSpeech("no audio tracks found in media")

    track = tracks.select(
        audio_tracks,
        target_lang=req.target_lang,
        source_priority=req.source_lang_priority,
        skip_if_target_audio_exists=req.skip_if_target_audio_exists,
    )
    if track is None:
        raise NoSuitableTrack(
            "no suitable source track (target-language audio already exists or only junk tracks present)"
        )

    # Pre-pass language detection: when the source track has no language
    # tag AND the configured Whisper backend is openvino (which can't
    # surface its own auto-detection), run faster-whisper-tiny on the
    # first 30s to nail down the language. Without this, NLLB and DeepL
    # would get fed a wrong source_lang and produce garbage on untagged
    # foreign-language tracks. The CPU backend (faster-whisper) detects
    # internally during the main transcribe call, so we skip the pre-pass
    # there.
    needs_detection_pre_pass = (
        track.language is None
        and settings.whisper_backend.lower() == "openvino"
    )

    with audio.extract_audio(req.media_path, track.index) as wav_path:
        language_hint = track.language
        if needs_detection_pre_pass:
            from app.pipeline import lang_detect
            detected = lang_detect.detect(wav_path)
            if detected:
                language_hint = detected
        transcription = stt.transcribe(wav_path, language_hint=language_hint)

    if not transcription.cues:
        raise NoSpeech(f"no speech detected in track {track.index}")

    context = _build_context(req, fp, transcription.cues) if req.mode in MULTIMODAL_MODES else None

    try:
        provider = get_provider(req.translation_provider)
    except ValueError as e:
        raise BadRequest(str(e)) from e

    try:
        translated = provider.translate(
            transcription.cues,
            transcription.detected_language,
            req.target_lang,
            context=context,
        )
    except TranslationError as e:
        raise TranslationFailed(str(e)) from e

    vtt = to_webvtt(
        translated,
        header_note=(
            f"Subtitle This auto-subs ({transcription.detected_language} -> {req.target_lang}, "
            f"mode={req.mode}, whisper={settings.whisper_model}, provider={req.translation_provider})"
        ),
    )

    payload = {
        "vtt": vtt,
        "source_track": {"index": track.index, "language": track.language, "title": track.title},
        "detected_source_language": transcription.detected_language,
        "cue_count": len(translated),
        "mode": req.mode,
    }
    cache_mod.store(key, payload)

    return ProcessResult(
        vtt=vtt,
        source_track_index=track.index,
        source_track_language=track.language,
        source_track_title=track.title,
        detected_source_language=transcription.detected_language,
        cue_count=len(translated),
        mode=req.mode,
        cached=False,
        took_seconds=time.monotonic() - started,
    )


def _build_context(
    req: ProcessRequest,
    fp: str,
    cues: list[stt.Cue],
) -> TranslationContext:
    """Build the bible (cached) and, for cinematic, also extract per-cue frames."""
    bible = scene_bible.load_cached_bible(fp)
    if bible is None:
        scene_list = scenes.detect_scenes(
            req.media_path,
            threshold=settings.scene_detection_threshold,
            min_length_seconds=settings.scene_min_length_seconds,
            max_scenes=settings.scene_max_scenes,
        )
        if not scene_list:
            # No scenes detected — fall back to plain translation rather than
            # erroring. The translator just won't have visual context.
            return TranslationContext()

        keyframes: dict[int, bytes] = {}
        for scene in scene_list:
            ts = scenes.keyframe_timestamp(scene, settings.scene_keyframe_position)
            try:
                keyframes[scene.index] = frames.extract_frame_bytes(
                    req.media_path, ts, settings.scene_frame_max_size
                )
            except subprocess.CalledProcessError:
                continue

        scene_bible.describe_scenes(scene_list, keyframes)
        scene_bible.store_cached_bible(fp, scene_list)
        bible = scene_list

    cue_to_scene = scenes.map_cues_to_scenes(cues, bible)
    scene_infos = [
        SceneInfo(index=s.index, start=s.start, end=s.end, description=s.description or "")
        for s in bible if s.description
    ]

    cue_frames: dict[int, bytes] = {}
    if req.mode == "cinematic":
        for cue in cues:
            ts = (cue.start + cue.end) / 2.0
            try:
                cue_frames[cue.id] = frames.extract_frame_bytes(
                    req.media_path, ts, settings.cinematic_frame_max_size
                )
            except subprocess.CalledProcessError:
                continue   # one missing frame doesn't doom the whole job

    return TranslationContext(
        scenes=scene_infos, cue_to_scene=cue_to_scene, cue_frames=cue_frames
    )
