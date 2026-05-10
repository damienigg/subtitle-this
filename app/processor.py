"""Core pipeline orchestrator.

Subtitle creation is exclusively a manual per-item or per-batch user
action — there is no auto-trigger, no path-based CLI flow, and no
whole-library sweep. The job runner (queued via the UI's per-item
"Subtitle this" button or the batch flow on the Library page) calls
into here, optionally passing progress + cancel callbacks so the UI can
show a live progress bar and respect cancel clicks.

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
from typing import Callable

from app import cache as cache_mod
from app.config import settings
from app.pipeline import audio, frames, scene_bible, scenes, stt, tracks
from app.pipeline.translate import TranslationError, get_provider
from app.pipeline.translate.base import SceneInfo, TranslationContext
from app.pipeline.vtt import to_webvtt


# Progress callback contract: receives (pct ∈ [0, 100], stage_label). Total
# pipeline budget allocation:
#   0–3   extracting audio
#   3–8   detecting language (only when track is untagged + openvino backend)
#   8–80  transcribing       (the long pole)
#   80–98 translating
#   98–100 writing
# Cache hits jump to 100 immediately. The processor passes a sub-callback to
# transcribe() / translate() that maps their internal 0–1 progress onto the
# stage's slot in this overall budget.
ProgressCB = Callable[[float, str], None]
CancelCB = Callable[[], None]


def _noop_progress(pct: float, stage: str) -> None: ...
def _noop_cancel() -> None: ...


def _scaled(progress: ProgressCB, *, base: float, span: float, stage: str) -> Callable[[float], None]:
    """Adapter: takes a sub-task's 0–1 fractional progress and reports it as
    `base + frac * span` to the outer progress callback under `stage`. So if
    transcribe says it's 50% done, the outer pipeline reports 8 + 0.5 * 72 =
    44% under stage='transcribing'."""
    def cb(frac: float) -> None:
        progress(base + max(0.0, min(1.0, frac)) * span, stage)
    return cb


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


def validate_mode_provider_combo(mode: str, translation_provider: str) -> None:
    """The mode/provider invariants checkable without touching the media
    file or the network. Single source of truth — used eagerly at job
    submission (so the UI surfaces the error immediately on click) AND
    defensively inside process() (so any settings drift between submission
    and execution surfaces as the same error rather than a silent garbage
    output). Raises BadRequest with the same message either way.

    Heavier validation (vision-LLM api_key/endpoint presence) lives only
    in process() since it's specific to actually building a client.
    """
    if mode not in SUPPORTED_MODES:
        raise BadRequest(f"unknown mode {mode!r} (expected one of {SUPPORTED_MODES})")
    if mode in MULTIMODAL_MODES:
        if translation_provider != "llm":
            raise BadRequest(
                f"mode={mode!r} requires translation_provider='llm' "
                "(only the LLM provider talks to a multimodal backend)."
            )
        if not settings.vision_llm_enabled:
            raise BadRequest(
                f"mode={mode!r} requires the Vision LLM to be enabled. Configure a "
                "vision-capable model in Settings → Vision model and toggle vision_llm_enabled."
            )
        if mode == "cinematic" and not settings.translation_llm_supports_vision:
            raise BadRequest(
                "cinematic mode also attaches per-cue frames to the translation LLM. "
                "Enable translation_llm_supports_vision in Settings, pick a vision-capable "
                "translation model, or use scene mode instead."
            )


def process(
    req: ProcessRequest,
    *,
    progress: ProgressCB = _noop_progress,
    check_cancel: CancelCB = _noop_cancel,
) -> ProcessResult:
    started = time.monotonic()

    validate_mode_provider_combo(req.mode, req.translation_provider)
    check_cancel()
    if req.mode in MULTIMODAL_MODES:
        # Validate the configured vision LLM has its credentials. This is
        # process()-only (not in the shared validator) because it requires
        # poking at the active Vision LLM type configuration, which is more
        # detail than the submission-time fast-fail needs.
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

    media = Path(req.media_path)
    if not media.exists():
        raise MediaNotFound(f"media not found: {req.media_path}")

    # The cache key is built from every input that affects output:
    # - For scene/cinematic, the detection threshold + the vision LLM model
    #   (the bible depends on it).
    # - For provider=llm, the translation LLM model (output depends on it
    #   directly). DeepL/NLLB don't have a model setting that varies, so we
    #   pass None and they fall through to a single key per provider.
    # - For the OpenVINO STT backend, vad_enabled materially changes the cue
    #   list (silence-region hallucinations vs. clean output). The CPU
    #   backend's VAD is internal to faster-whisper and unrelated, so we
    #   pass None for it and avoid spuriously invalidating CPU cache entries
    #   when this flag is toggled.
    threshold = settings.scene_detection_threshold if req.mode in MULTIMODAL_MODES else None
    tllm_model = (
        settings.translation_llm_model if req.translation_provider == "llm" else None
    )
    vllm_model = (
        settings.vision_llm_model if req.mode in MULTIMODAL_MODES else None
    )
    vad_enabled_key = (
        settings.vad_enabled if settings.whisper_backend.lower() == "openvino" else None
    )
    key_kwargs = dict(
        target_lang=req.target_lang,
        model=settings.whisper_model,
        provider=req.translation_provider,
        source_priority=req.source_lang_priority,
        mode=req.mode,
        scene_threshold=threshold,
        translation_llm_model=tllm_model,
        vision_llm_model=vllm_model,
        vad_enabled=vad_enabled_key,
    )
    # Two-level cache: the quick (path+mtime) fingerprint is the hot path;
    # the content (mid-file bytes) fingerprint is a stable fallback that
    # survives mtime bumps from rsync/sync tools and the mkvpropedit write-
    # back step. On a content-fp hit, lookup_two_level re-links the payload
    # under the current quick key so the next run hits the fast path.
    cached, quick_key, content_key = cache_mod.lookup_two_level(media, **key_kwargs)
    if cached:
        progress(100, "cache hit")
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

    progress(0, "extracting audio")
    check_cancel()
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
        progress(3, "detecting language" if needs_detection_pre_pass else "transcribing")
        check_cancel()
        language_hint = track.language
        if needs_detection_pre_pass:
            from app.pipeline import lang_detect
            detected = lang_detect.detect(wav_path)
            if detected:
                language_hint = detected
            progress(8, "transcribing")
            check_cancel()
        transcription = stt.transcribe(
            wav_path,
            language_hint=language_hint,
            progress=_scaled(progress, base=8, span=72, stage="transcribing"),
            check_cancel=check_cancel,
        )

    if not transcription.cues:
        raise NoSpeech(f"no speech detected in track {track.index}")
    progress(80, "translating")
    check_cancel()

    # For scene/cinematic, use the CONTENT fingerprint (not the quick one)
    # to key the on-disk scene bible. The bible depends on the visual
    # content of the film — mtime bumps and metadata-only edits don't
    # invalidate it, so we want the bible cache to survive those too.
    context = (
        _build_context(
            req, cache_mod.content_fingerprint(media), transcription.cues,
            check_cancel=check_cancel,
        )
        if req.mode in MULTIMODAL_MODES else None
    )

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
            progress=_scaled(progress, base=80, span=18, stage="translating"),
            check_cancel=check_cancel,
        )
    except TranslationError as e:
        raise TranslationFailed(str(e)) from e
    progress(98, "writing")

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
    # Store under both fingerprints so any future lookup — by quick fp or
    # by content fp after an mtime bump — retrieves the payload.
    #
    # CACHE SAFETY GUARANTEE: this write only runs after `provider.translate`
    # has returned successfully. Any `check_cancel()` along the pipeline
    # raises `JobCanceled` BEFORE this point, so a canceled job never leaves
    # a partial or stale entry behind. Re-running the same item after a
    # cancel always recomputes from scratch — there is no "fucked-up cached
    # half-result" path. test_cancel_does_not_leave_cache_entry locks this in.
    cache_mod.store_two_level(media, payload, **key_kwargs)

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
    *,
    check_cancel: CancelCB = _noop_cancel,
) -> TranslationContext:
    """Build the bible (cached) and, for cinematic, also extract per-cue frames.

    Cancel checks are interleaved between scene-detection / frame-extraction /
    LLM-bible-build / per-cue frame extraction so a cancel click during the
    minutes-long bible build takes effect promptly. Critically, the bible is
    only `store_cached_bible`-d AFTER `describe_scenes` returns successfully,
    so a cancel mid-build leaves nothing on disk."""
    bible = scene_bible.load_cached_bible(fp)
    if bible is None:
        check_cancel()
        scene_list = scenes.detect_scenes(
            req.media_path,
            threshold=settings.scene_detection_threshold,
            min_length_seconds=settings.scene_min_length_seconds,
            max_scenes=settings.scene_max_scenes,
            check_cancel=check_cancel,
        )
        if not scene_list:
            # No scenes detected — fall back to plain translation rather than
            # erroring. The translator just won't have visual context.
            return TranslationContext()

        keyframes: dict[int, bytes] = {}
        for scene in scene_list:
            check_cancel()
            ts = scenes.keyframe_timestamp(scene, settings.scene_keyframe_position)
            try:
                keyframes[scene.index] = frames.extract_frame_bytes(
                    req.media_path, ts, settings.scene_frame_max_size
                )
            except subprocess.CalledProcessError:
                continue

        scene_bible.describe_scenes(scene_list, keyframes, check_cancel=check_cancel)
        scene_bible.store_cached_bible(fp, scene_list)
        bible = scene_list

    cue_to_scene = scenes.map_cues_to_scenes(cues, bible)
    scene_infos = [
        SceneInfo(index=s.index, start=s.start, end=s.end, description=s.description or "")
        for s in bible if s.description
    ]

    cue_ids_with_frames: set[int] | None = None
    cue_frames_provider = None
    if req.mode == "cinematic":
        # Frame extraction is now LAZY + CAPPED. Previously we pre-extracted
        # one JPEG per cue here and held the entire dict in RAM through the
        # translation phase — for a 2 h film with heavy dialog that's
        # 1500+ JPEGs ≈ 200-300 MB resident, plus base64 inflation per LLM
        # call. With heavy use of cinematic mode this peak is what blew up
        # the TrueNAS host. Two changes:
        #
        # 1. Cap (settings.cinematic_max_cues_with_frames): only the first N
        #    cues get a frame. The remainder still translate — they just go
        #    through the text-only path. Set the cap to 0 to disable per-cue
        #    frames entirely (effectively downgrading cinematic to scene).
        # 2. Lazy: instead of building a {cue_id: bytes} dict here, we hand
        #    the translator a closure that calls ffmpeg per-cue at the
        #    moment the frame is needed. The translator extracts a batch's
        #    worth of frames at a time — peak RAM per batch is now
        #    cinematic_batch_size frames, not len(cues).
        cap = max(0, int(settings.cinematic_max_cues_with_frames or 0))
        if cap > 0:
            cue_ids_with_frames = {c.id for c in cues[:cap]}
            media_path = req.media_path
            frame_max = settings.cinematic_frame_max_size
            accurate = bool(settings.cinematic_frame_accurate_seek)
            # Build a cue.id → cue index for O(1) lookup in the provider.
            # The provider fires once per (cue.id, batch) — without the
            # index we'd O(N) scan `cues` for every frame; on a 1500-cue
            # film with a 10-cue batch size that's 1500×10 = 15k scans.
            cue_by_id = {c.id: c for c in cues}

            def _extract_one(cue_id: int) -> bytes | None:
                c = cue_by_id.get(cue_id)
                if c is None:
                    return None
                ts = (c.start + c.end) / 2.0
                try:
                    return frames.extract_frame_bytes(
                        media_path, ts, frame_max, accurate=accurate,
                    )
                except subprocess.CalledProcessError:
                    return None   # one missing frame doesn't doom the job

            cue_frames_provider = _extract_one

    return TranslationContext(
        scenes=scene_infos,
        cue_to_scene=cue_to_scene,
        cue_frames={},   # eager dict stays empty — provider does the work
        cue_frames_provider=cue_frames_provider,
        cue_ids_with_frames=cue_ids_with_frames,
    )
