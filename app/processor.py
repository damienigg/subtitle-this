"""Core pipeline orchestrator.

Subtitle creation is exclusively a manual per-item or per-batch user
action — there is no auto-trigger, no path-based CLI flow, and no
whole-library sweep. The job runner (queued via the UI's per-item
"Subtitle this" button or the batch flow on the Library page) calls
into here, optionally passing progress + cancel callbacks so the UI can
show a live progress bar and respect cancel clicks.

Pipeline shape (single audio-only mode since 0.7.32):

    [optional vocal isolation] → Whisper STT → translator → VTT

Pre-0.7.32 there were also "scene" and "cinematic" multimodal modes
that ran a Vision LLM on extracted keyframes (+ per-cue frames for
cinematic) to give the translator visual context. Those were removed
along with the supporting pipeline modules (scenes / scene_bible /
frames) — the modes added significant UI/code complexity for marginal
subtitle-quality improvement, and the same disambiguation usually
falls out of the surrounding dialog Whisper already captures.

Errors are raised as typed subclasses of ProcessError so callers can map them
to specific HTTP status codes without resorting to string matching.
"""
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_log = logging.getLogger("subtitle_this")

from app import cache as cache_mod
from app.config import settings
from app.pipeline import audio, stt, tracks
from app.pipeline.translate import TranslationError, get_provider
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
    skip_if_target_audio_exists: bool = True


@dataclass
class ProcessResult:
    vtt: str
    source_track_index: int
    source_track_language: str | None
    source_track_title: str | None
    detected_source_language: str
    cue_count: int
    cached: bool
    took_seconds: float
    # Optional per-run pipeline telemetry — VAD coverage, packing pad-
    # drops, whisper-degenerate-timestamp counts. None when served
    # from a cache hit that pre-dates the telemetry-bearing payload,
    # populated otherwise. Surfaced on the Cache Explorer stats page.
    pipeline_metrics: "dict | None" = None


def process(
    req: ProcessRequest,
    *,
    progress: ProgressCB = _noop_progress,
    check_cancel: CancelCB = _noop_cancel,
) -> ProcessResult:
    started = time.monotonic()

    check_cancel()

    media = Path(req.media_path)
    if not media.exists():
        raise MediaNotFound(f"media not found: {req.media_path}")

    # The cache key is built from every input that affects output:
    # - For provider=llm, the translation LLM model (output depends on
    #   it directly). DeepL/NLLB don't have a model setting that
    #   varies, so we pass None and they fall through to a single key
    #   per provider.
    # - For the OpenVINO STT backend, vad_enabled materially changes
    #   the cue list (silence-region hallucinations vs. clean output).
    #   The CPU backend's VAD is internal to faster-whisper and
    #   unrelated, so we pass None for it and avoid spuriously
    #   invalidating CPU cache entries when this flag is toggled.
    tllm_model = (
        settings.translation_llm_model if req.translation_provider == "llm" else None
    )
    vad_enabled_key = (
        settings.vad_enabled if settings.whisper_backend.lower() == "openvino" else None
    )
    key_kwargs = dict(
        target_lang=req.target_lang,
        model=settings.whisper_model,
        provider=req.translation_provider,
        source_priority=req.source_lang_priority,
        translation_llm_model=tllm_model,
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
            cached=True,
            took_seconds=time.monotonic() - started,
            pipeline_metrics=cached.get("pipeline_metrics"),
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

    # Intermediate transcript cache: the long pole of the pipeline is
    # Whisper (8-80% of the budget). If a previous run made it past
    # transcription but crashed during translation (OOM, transient
    # provider error, container restart), we have the cues on disk and
    # can skip Whisper entirely on retry — jumping straight to the
    # translation phase. Keyed only on STT-relevant inputs, so changing
    # target_lang / provider / mode also hits the cached transcript.
    from app import transcript_cache
    transcript_content_fp = cache_mod.content_fingerprint(media)
    # Derive the on/off bool used by the cache key from the tri-state
    # mode setting. The cache treats FULL and CHUNKED as equivalent
    # (both produce vocals-isolated audio; the sub-second seam
    # artifacts between them don't change Whisper's cue extraction).
    vocal_isolation_enabled = settings.vocal_isolation_mode != "off"
    transcription = transcript_cache.lookup(
        transcript_content_fp,
        settings.whisper_model,
        settings.whisper_backend,
        settings.vad_enabled,
        track.index,
        vocal_isolation_enabled=vocal_isolation_enabled,
    )
    transcript_from_cache = transcription is not None
    # Captures the Demucs metrics so the stats page can show "isolation
    # ran for N seconds, output spent 0 RAM during STT". None when the
    # phase didn't run this job — either disabled, or cache hit.
    vocal_isolation_metrics = None
    if transcript_from_cache:
        # Hit: skip audio extraction + isolation + Whisper. Jump the
        # progress bar straight to the start of translation so the user
        # sees the phase change immediately.
        progress(80, "translating (transcript cache hit)")
        check_cancel()
    else:
        # Choose the audio prep path:
        # - 5.1+ source: center-channel extraction in audio.extract_audio
        #   gives us dialogue-only audio for free (~5 s of ffmpeg work).
        #   Demucs would be redundant AND slower (~15-30 min), so we
        #   skip it even when vocal_isolation_mode != "off". The user's
        #   isolation toggle stays useful for stereo sources.
        # - Stereo/mono source with vocal_isolation enabled: Demucs path.
        # - Else: standard mono downmix.
        # Both yield the same shape — a 16 kHz mono WAV at a Path. STT
        # downstream doesn't care which one produced it.
        channel_info = audio.probe_channel_layout(req.media_path, track.index)
        use_demucs = vocal_isolation_enabled and not channel_info.has_center
        if vocal_isolation_enabled and channel_info.has_center:
            _log.info(
                "audio prep: %d-channel source detected → skipping Demucs in "
                "favour of center-channel extraction. The FC channel is "
                "dialogue-only by mix convention, so isolation is redundant.",
                channel_info.channels,
            )
        # AudioPrepMetrics sink — populated by extract_audio (or, for
        # the Demucs path, synthesised from channel_info below). We
        # build the metrics record after the extraction context exits
        # so the operator sees both the pre-decision channel info and
        # whether the optimised filter chain had to bail out at runtime.
        prep_stats: dict = {
            "source_channels": channel_info.channels,
            "source_channel_layout": channel_info.layout,
            "used_center_channel": False,
            "loudnorm_applied": False,
            "optimised_chain_failed": False,
            # True when settings.vocal_isolation_mode != "off" but we
            # skipped Demucs because the source is 5.1+ (FC pan is
            # cheaper and produces cleaner dialogue-only audio). The
            # stats page surfaces this so the operator understands
            # why the Vocal isolation block they enabled isn't visible.
            "vocal_isolation_auto_skipped": (
                vocal_isolation_enabled and channel_info.has_center
            ),
        }
        if use_demucs:
            from app.pipeline import vocal_isolation as vi
            audio_ctx = vi.isolate_vocals(
                req.media_path, track.index,
                progress=_scaled(progress, base=0, span=8, stage="isolating vocals"),
                check_cancel=check_cancel,
            )
            # Demucs path uses its own ffmpeg invocation with loudnorm
            # applied, but skips the FC-pan optimisation (vocals come
            # from the model output, not from a single channel).
            prep_stats["loudnorm_applied"] = True
        else:
            audio_ctx = audio.extract_audio(
                req.media_path, track.index, prep_stats=prep_stats,
            )

        with audio_ctx as audio_handle:
            # vocal_isolation yields an IsolationResult; audio.extract_audio
            # yields a bare Path. Normalize to a Path here so the STT call
            # is identical for both paths.
            if hasattr(audio_handle, "wav_path"):
                wav_path = audio_handle.wav_path
                from app.pipeline_metrics import VocalIsolationMetrics
                rt = (
                    audio_handle.audio_seconds_processed
                    / audio_handle.took_seconds
                    if audio_handle.took_seconds > 0 else 0.0
                )
                vocal_isolation_metrics = VocalIsolationMetrics(
                    enabled=True,
                    model=audio_handle.model,
                    took_seconds=audio_handle.took_seconds,
                    audio_seconds_processed=audio_handle.audio_seconds_processed,
                    realtime_factor=round(rt, 2),
                )
            else:
                wav_path = audio_handle

            progress(8 if use_demucs else 3,
                     "detecting language" if needs_detection_pre_pass else "transcribing")
            check_cancel()
            language_hint = track.language
            if needs_detection_pre_pass:
                from app.pipeline import lang_detect
                detected = lang_detect.detect(wav_path)
                if detected:
                    language_hint = detected
                progress(10 if use_demucs else 8, "transcribing")
                check_cancel()
            stt_base = 10 if use_demucs else 8
            stt_span = 80 - stt_base
            transcription = stt.transcribe(
                wav_path,
                language_hint=language_hint,
                progress=_scaled(progress, base=stt_base, span=stt_span, stage="transcribing"),
                check_cancel=check_cancel,
            )

        # Confidence-gated re-transcription pass (0.8.0). Walks the
        # first-pass cue list, identifies 10-min audio buckets that are
        # weak (coverage < 30 % OR mean avg_logprob < -1.0), and re-
        # decodes those ranges with aggressive params. Safety: capped
        # at 20 % of audio re-passed, re-uses the cached Whisper model
        # (no double-load), keeps first-pass result if the re-pass
        # produces fewer cues. See stt_refine.py for the full safety
        # contract. Runs BEFORE anti-hallucination so the filter
        # operates on the post-refine cue list (catches any new
        # hallucinations the aggressive re-pass produced).
        from app.pipeline import stt_refine
        # Audio duration estimate: max cue end OR known WAV length.
        # If the wav_path is still resident (it's not — the with-block
        # closed it above), we'd ffprobe it; using cue end is good
        # enough for bucket boundaries.
        if transcription.cues:
            audio_dur = max(c.end for c in transcription.cues)
            transcription, refine_stats = stt_refine.refine_weak_buckets(
                transcription,
                req.media_path,
                track.index,
                audio_dur,
                language_hint=transcription.detected_language,
                progress=_scaled(progress, base=78, span=2, stage="refining"),
                check_cancel=check_cancel,
            )
            from app import pipeline_metrics as pm_mod
            if transcription.pipeline_metrics is None:
                transcription.pipeline_metrics = pm_mod.PipelineMetrics()
            if transcription.pipeline_metrics.whisper is None:
                transcription.pipeline_metrics.whisper = pm_mod.WhisperMetrics()
            transcription.pipeline_metrics.whisper.refine = pm_mod.RefineMetrics(
                buckets_evaluated=refine_stats.buckets_evaluated,
                buckets_weak=refine_stats.buckets_weak,
                buckets_refined=refine_stats.buckets_refined,
                cues_added=refine_stats.cues_added,
                cues_replaced=refine_stats.cues_replaced,
                audio_seconds_refined=refine_stats.audio_seconds_refined,
                skipped_reason=refine_stats.skipped_reason,
            )

        # Anti-hallucination pass on the (post-refine) cue list. Drops
        # the YouTube-corpus-tail signature phrases ("Thanks for
        # watching.", "Subscribe.", etc.) that Whisper falls back to
        # on silence, plus stuck-loop repetitions ("yeah yeah yeah
        # yeah"). See anti_hallucination.py for the full list +
        # rationale. Cue ids are renumbered to stay contiguous
        # post-filter.
        from app.pipeline import anti_hallucination
        filtered_cues, ah_stats = anti_hallucination.filter_cues(transcription.cues)
        transcription.cues = filtered_cues
        from app import pipeline_metrics as pm_mod
        if transcription.pipeline_metrics is None:
            transcription.pipeline_metrics = pm_mod.PipelineMetrics()
        transcription.pipeline_metrics.anti_hallucination = pm_mod.AntiHallucinationMetrics(
            input_count=ah_stats.input_count,
            blacklist_dropped=ah_stats.blacklisted,
            repetition_dropped=ah_stats.repetition_dropped,
            output_count=ah_stats.output_count,
            safety_bailout=ah_stats.safety_bailout,
        )
        # Keep the legacy whisper.hallucinations_dropped summary populated
        # for backwards-compatibility with any consumer that reads it
        # directly (quality.py, older cache_stats template versions).
        if ah_stats.blacklisted or ah_stats.repetition_dropped:
            if transcription.pipeline_metrics.whisper is None:
                transcription.pipeline_metrics.whisper = pm_mod.WhisperMetrics()
            transcription.pipeline_metrics.whisper.hallucinations_dropped = (
                ah_stats.blacklisted + ah_stats.repetition_dropped
            )

        # Store BEFORE the translation phase begins. If translation crashes
        # mid-flight, the next retry hits this entry and skips the 30+ min
        # of Whisper. Empty-cue transcriptions are not stored (transcript_cache
        # handles that internally).
        # Fold the isolation + audio-prep metrics into
        # transcription.pipeline_metrics so they ride into the transcript
        # cache and the stats sidecar alongside VAD/packing/whisper
        # telemetry.
        from app import pipeline_metrics as pm_mod
        if transcription.pipeline_metrics is None:
            transcription.pipeline_metrics = pm_mod.PipelineMetrics()
        transcription.pipeline_metrics.audio_prep = pm_mod.AudioPrepMetrics(
            source_channels=int(prep_stats.get("source_channels") or 0),
            source_channel_layout=prep_stats.get("source_channel_layout"),
            used_center_channel=bool(prep_stats.get("used_center_channel")),
            loudnorm_applied=bool(prep_stats.get("loudnorm_applied")),
            optimised_chain_failed=bool(prep_stats.get("optimised_chain_failed")),
            vocal_isolation_auto_skipped=bool(
                prep_stats.get("vocal_isolation_auto_skipped")
            ),
        )
        if vocal_isolation_metrics is not None:
            transcription.pipeline_metrics.vocal_isolation = vocal_isolation_metrics
        transcript_cache.store(
            transcript_content_fp,
            settings.whisper_model,
            settings.whisper_backend,
            settings.vad_enabled,
            track.index,
            transcription,
            vocal_isolation_enabled=vocal_isolation_enabled,
        )

    if not transcription.cues:
        raise NoSpeech(f"no speech detected in track {track.index}")

    # Release the STT model BEFORE the translation phase loads its own
    # weights. With whisper-small (~1 GB) + NLLB-600M (~1.5 GB) + Python
    # overhead + torch pools + page cache of the mmap'd model files,
    # holding both resident simultaneously pushes a 6 GB-capped container
    # past the cgroup limit and the kernel SIGKILLs the process at the
    # 80% mark — no Python traceback, no error on the job, just a job
    # that silently stops producing a .vtt. (Real incident: 2026-05 OOM
    # on TrueNAS with cgroup mem_limit=6g, anon-rss=3.77 GB + mmap'd
    # weights = past the cap right when NLLB initialized.)
    #
    # Skip the release dance entirely on a transcript-cache hit — we
    # never loaded Whisper this run, so there's nothing to free.
    if not transcript_from_cache:
        if needs_detection_pre_pass:
            from app.pipeline import lang_detect
            lang_detect.release_detector()
        stt.release()
        progress(80, "translating")
    check_cancel()

    def _translation_model_id(provider_name: str) -> str | None:
        """Return the human-readable model identifier for the active
        translation provider. Surfaced on the stats page so the user
        sees WHICH model produced the output without having to cross-
        reference settings — relevant when comparing two runs that
        differ only in translation backend."""
        p = (provider_name or "").lower()
        if p == "nllb":
            return settings.nllb_model
        if p == "llm":
            return settings.translation_llm_model
        if p == "deepl":
            return "deepl"   # DeepL doesn't expose a model name
        return None

    try:
        provider = get_provider(req.translation_provider)
    except ValueError as e:
        raise BadRequest(str(e)) from e

    translate_started = time.monotonic()
    try:
        translated = provider.translate(
            transcription.cues,
            transcription.detected_language,
            req.target_lang,
            progress=_scaled(progress, base=80, span=18, stage="translating"),
            check_cancel=check_cancel,
        )
    except TranslationError as e:
        raise TranslationFailed(str(e)) from e
    translate_took = time.monotonic() - translate_started
    progress(98, "writing")

    # ── Translation metrics ──────────────────────────────────────────────
    # Computed from outside the provider so the same code works for
    # NLLB, DeepL, and the LLM backends. Adds about a millisecond on a
    # 2 h film — pure dict/counter work.
    from app import pipeline_metrics as pm_mod
    translation_metrics = pm_mod.compute_translation_metrics(
        provider=req.translation_provider,
        model=_translation_model_id(req.translation_provider),
        input_cues=transcription.cues,
        output_cues=translated,
        took_seconds=translate_took,
    )
    # Merge into the transcription's pipeline_metrics so everything
    # flows through ONE struct down to the cache payload + .stats.json.
    if transcription.pipeline_metrics is None:
        transcription.pipeline_metrics = pm_mod.PipelineMetrics()
    transcription.pipeline_metrics.translation = translation_metrics

    # ── Readability polish ──────────────────────────────────────────────
    # Raw Whisper timing produces ~40 % of cues under 1 s on a typical
    # talky film — too brief to read. The polish pass extends short
    # cues (capped to never overlap the next one), and optionally
    # merges adjacent fragments that visually read as one subtitle.
    # No-op when settings.polish_enabled is False; otherwise gated
    # by per-knob settings. Operates on the translated cue list so
    # reading-speed math uses the target-language text length.
    from app.pipeline.polish import polish_cues_with_stats
    polish_applied = bool(settings.polish_enabled)
    translated, polish_stats = polish_cues_with_stats(translated)
    transcription.pipeline_metrics.polish = pm_mod.PolishMetrics(
        enabled=polish_stats.enabled,
        input_count=polish_stats.input_count,
        output_count=polish_stats.output_count,
        cues_merged=polish_stats.cues_merged,
        cues_extended=polish_stats.cues_extended,
    )

    # The NOTE header in the .vtt records provenance so a downstream
    # viewer (Cache Explorer / stats page / a human reading the file)
    # can tell at a glance which pipeline produced it. ``polished=true``
    # is the marker for "readability polish was applied" — pre-0.7.20
    # entries and any post-0.7.20 entry that ran with polish_enabled
    # = false will lack the suffix, which the UI surfaces as a
    # "raw timing" indicator.
    note_body = (
        f"Subtitle This auto-subs ({transcription.detected_language} -> {req.target_lang}, "
        f"whisper={settings.whisper_model}, provider={req.translation_provider}"
    )
    if polish_applied:
        note_body += ", polished=true"
    note_body += ")"

    vtt = to_webvtt(translated, header_note=note_body)

    # Serialize pipeline_metrics for the cache payload (and for the
    # ProcessResult). The transcription struct holds them as a
    # PipelineMetrics dataclass — flatten to a JSON-safe dict here so
    # both the on-disk cache and a re-run from a cache hit can carry
    # the same structure forward without re-running STT.
    pipeline_metrics_dict: dict | None = None
    if transcription.pipeline_metrics is not None:
        from app import pipeline_metrics as pm_mod
        pipeline_metrics_dict = pm_mod.to_jsonable(transcription.pipeline_metrics)

    payload = {
        "vtt": vtt,
        # media_path lets the Cache Explorer UI render the film name without
        # parsing the .vtt header. Older payloads (pre-0.7.4) don't carry it
        # and fall back to whatever the NOTE line exposes.
        "media_path": str(media),
        "source_track": {"index": track.index, "language": track.language, "title": track.title},
        "detected_source_language": transcription.detected_language,
        "cue_count": len(translated),
        # Pipeline telemetry — None on legacy cache hits and on the
        # CPU/faster-whisper backend (only the OpenVINO path instruments
        # VAD / packing today). Consumers must tolerate the absence.
        "pipeline_metrics": pipeline_metrics_dict,
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

    # Write the .stats.json sidecar INSIDE the cache (cache_dir/stats/),
    # NOT next to the .vtt — the .vtt sits in the user's movie folder
    # and they don't want metrics polluting it. Two files written so
    # both the quick-fp and content-fp lookups can find a paired
    # sidecar; the payloads are identical, taking ~few KB total.
    try:
        from app import stats as stats_mod
        stats_record = stats_mod.compute_from_vtt(
            vtt,
            media_path=str(media),
            detected_source_language=transcription.detected_language,
            took_seconds=time.monotonic() - started,
            pipeline_metrics=pipeline_metrics_dict,
        )
        content_fp = cache_mod.content_fingerprint(media)
        quick_fp = cache_mod.quick_fingerprint(media)
        quick_key_for_stats = cache_mod.cache_key(quick_fp, **key_kwargs)
        content_key_for_stats = cache_mod.cache_key(content_fp, **key_kwargs)
        stats_mod.write_cache_sidecar(quick_key_for_stats, stats_record)
        if quick_key_for_stats != content_key_for_stats:
            stats_mod.write_cache_sidecar(content_key_for_stats, stats_record)
    except Exception:
        # Stats are observability, not a correctness requirement —
        # never let a metrics write failure abort the job.
        _log.warning("stats sidecar write failed", exc_info=True)

    return ProcessResult(
        vtt=vtt,
        source_track_index=track.index,
        source_track_language=track.language,
        source_track_title=track.title,
        detected_source_language=transcription.detected_language,
        cue_count=len(translated),
        cached=False,
        took_seconds=time.monotonic() - started,
        pipeline_metrics=pipeline_metrics_dict,
    )


