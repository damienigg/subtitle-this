"""OpenVINO STT backend via optimum-intel.

Loads a Whisper checkpoint as OpenVINO IR and drives it via
`OVModelForSpeechSeq2Seq.generate()` directly, chunking the audio into
30-second windows ourselves.

Why direct generate() and not transformers.pipeline:
The HF AutomaticSpeechRecognitionPipeline kept tensor inputs on CPU
("Device set to use cpu" in the logs) while the OVModel ran on GPU.0,
causing a CPU↔iGPU round-trip per generated token for the logits
processors. On a 2h28 film with whisper-small that produced an effective
RTF of ~0.32 (47 min real time on N305 iGPU) — versus the ~0.07 the iGPU
is actually capable of when the pipeline overhead is gone. The transformers
docs themselves flag the chunk_length_s + seq2seq combo as experimental
and recommend going through model.generate() directly. So we do.

Silence handling — why VAD pre-filter:
Direct generate() bypasses Whisper's built-in no-speech / log-prob /
compression-ratio guards (those live in the reference pipeline, not in
the model). On silent windows the autoregressive decoder hallucinates
boilerplate from its language prior ("Thank you.", "Thanks for watching.",
repeats of recent lines). We pre-filter the audio with Silero-VAD and
chunk *within* speech regions only — silent stretches never reach the
decoder, killing the hallucinations and cutting compute by 30–50 % on a
typical film. See `app/pipeline/vad.py` for the rationale and trade-offs.

Trade-off: we do simple non-overlapping 30s chunking (no stride). Words
that straddle a chunk boundary may be split into two cues. Acceptable for
v1; worth revisiting only if users report visible artifacts.

Progress reporting is true per-chunk i/N — cancel between chunks is
responsive (<= 1 chunk's worth, typically 1-3s on iGPU).
"""
import logging
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Callable

# numpy + soundfile are imported lazily inside transcribe() so the pure
# parsing code (_parse_segments) is testable without those native deps.
# The CPU-only flavor of the dev image doesn't ship them.

from app.config import settings
from app.pipeline.openvino_introspect import log_selected_device
from app.pipeline.packing import (
    RegionEntry, Window, plan_packed_windows, remap_cue_to_original,
)
from app.pipeline.stt import Cue, TranscriptionResult
from app.pipeline.vad import detect_speech


_log = logging.getLogger("subtitle_this")
_HF_PREFIX = "openai/whisper-"
_CHUNK_SECONDS = 30
_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = _CHUNK_SECONDS * _SAMPLE_RATE

# How many 30s chunks to feed model.generate() per call. Each call has a
# fixed startup cost (decoder init, kv-cache alloc, scheduling to iGPU);
# batching N chunks into one call amortizes that cost and lets the iGPU
# process the batch in parallel. Sized per-model to stay under the iGPU's
# memory budget (whisper-large activations dominate at batch sizes > 2).
_BATCH_BY_MODEL: dict[str, int] = {
    "tiny": 8,
    "base": 8,
    "small": 4,
    "medium": 2,
    "large-v3-turbo": 2,
    "large-v3": 2,
}
_DEFAULT_BATCH = 4


def _noop_progress(frac: float) -> None: ...
def _noop_cancel() -> None: ...


@lru_cache(maxsize=1)
def _model_and_processor(model_name: str, device: str, cache_root: str):
    """Cache keyed by config so settings changes reload cleanly. Heavy
    imports stay inside so the CPU backend doesn't pay them at import time.

    maxsize=1 (not 2) so toggling whisper_model in the UI never leaves the
    previous model resident — Whisper-large weights are ~3 GB; keeping a
    spare doubles the resident set for no real workflow benefit. The
    lru_cache key already includes the model_name so cache hits across
    same-config jobs still work.

    Returns (OVModelForSpeechSeq2Seq, AutoProcessor)."""
    from optimum.intel import OVModelForSpeechSeq2Seq
    from transformers import AutoProcessor

    model_id = _HF_PREFIX + model_name
    cache_dir = Path(cache_root) / "openvino-models"
    cache_dir.mkdir(parents=True, exist_ok=True)

    processor = AutoProcessor.from_pretrained(model_id, cache_dir=str(cache_dir))
    model = OVModelForSpeechSeq2Seq.from_pretrained(
        model_id,
        export=True,
        device=device,
        cache_dir=str(cache_dir),
    )
    log_selected_device("whisper:" + model_name, requested=device, model=model)
    return model, processor


def release_model() -> None:
    """Evict the cached OpenVINO Whisper model from RAM.

    Called by processor.py between the STT and translation phases. The
    intent: keeping Whisper-small (~1 GB) resident while NLLB-600M
    (~1.5 GB) loads pushes a 6 GB-capped container past its cgroup limit
    and triggers a silent kernel OOM-kill at exactly the 80% mark of the
    pipeline. With this release, the next job pays a ~10-30s reload cost
    when transcribe() is called again — dwarfed by the actual decode work,
    and obviously much better than crashing.

    The cache_clear() drops the lru_cache's reference to (model, processor);
    gc.collect() walks any remaining cycles so the underlying OpenVINO
    CompiledModel destructor runs and releases the iGPU-reserved RAM.
    try_malloc_trim() then forces glibc to return the freed arenas to
    the kernel — without it the memory stays accounted against the
    cgroup even though Python is logically done with it, and a large
    follow-up allocation (NLLB load) can trip the OOM-killer.
    """
    import gc
    from app.pipeline.stt import try_malloc_trim
    _model_and_processor.cache_clear()
    gc.collect()
    try_malloc_trim()


# Whisper auto-emits language tokens like <|en|>, <|fr|>, <|ja|> at the start
# of the decoded output when no language is forced. This set lets us pick the
# real language token out without false-positive matching on control tokens
# like <|transcribe|> or <|notimestamps|>.
_LANG_TOKEN_RE = re.compile(
    r"<\|(en|fr|es|de|it|pt|ja|ko|zh|ru|ar|hi|tr|vi|th|pl|nl|sv|no|da|"
    r"fi|cs|el|he|hu|ro|uk|id|ms|tl|ca|bn|fa|ur|tg|sk|sl|et|lv|lt|bg|mk|"
    r"sr|hr|bs|sq|az|ka|hy|kk|ky|uz|mn|my|km|lo|am|ti|so|sw|yo|ig|ha)\|>"
)


def _parse_segments(
    decoded: str,
    time_offset_s: float,
    on_drop: Callable[[], None] | None = None,
) -> list[tuple[float, float, str]]:
    """Parse a Whisper decoded-with-timestamps string into [(start, end, text)].

    Whisper emits output as: <|0.00|>text<|2.50|>text<|5.00|>... — each
    pair of timestamp markers brackets a segment. We pair successive
    numeric markers and treat the text between them as the cue. Empty /
    whitespace-only segments and zero-duration markers are dropped.

    `time_offset_s` is added to every timestamp so chunked-mode callers
    (every 30s window) can produce globally-correct timestamps without
    re-walking the output.

    Defensive note: Whisper occasionally emits non-monotonic timestamps
    on heavy hallucination (end < start, or duplicated markers). We
    silently drop those pairs to keep the cue list well-formed, but log
    a debug-level counter on the module logger so regressions are
    visible in `docker logs` without spamming for the common no-drop case.
    """
    markers = list(re.finditer(r"<\|(\d+\.\d+)\|>", decoded))
    out: list[tuple[float, float, str]] = []
    dropped = 0
    for i in range(len(markers) - 1):
        m1, m2 = markers[i], markers[i + 1]
        start = float(m1.group(1)) + time_offset_s
        end = float(m2.group(1)) + time_offset_s
        text = decoded[m1.end():m2.start()].strip()
        if text and end > start:
            out.append((start, end, text))
        elif text:
            # We have text but the timing is degenerate — log it so a
            # regression that turns half the cues into degenerates is
            # visible rather than silently halving the output.
            dropped += 1
            if on_drop is not None:
                on_drop()
    if dropped:
        _log.debug(
            "_parse_segments dropped %d cue(s) with degenerate timestamps "
            "at offset=%.1fs (text present, end<=start)", dropped, time_offset_s,
        )
    return out


def transcribe(
    audio_path: Path,
    language_hint: str | None = None,
    *,
    progress: Callable[[float], None] = _noop_progress,
    check_cancel: Callable[[], None] = _noop_cancel,
) -> TranscriptionResult:
    """Transcribe `audio_path` (a 16 kHz mono WAV) via OpenVINO Whisper.

    Streaming + segmentation + packing layout (default config):

    1. **Segmentation**: audio is read in N-second segments (default 600 s).
       Peak audio RAM stays bounded — ~75 MB resident at any moment for
       16 kHz mono float32, instead of ~500 MB for the entire 2 h film.

    2. **Forward overlap**: each segment read pulls an extra
       `stt_segment_overlap_seconds` (default 30 s) past the segment
       boundary. Speech regions straddling the boundary are processed
       fully in segment N; segment N+1 skips ahead to where N stopped.
       Eliminates split-word artifacts at boundaries.

    3. **Region packing**: short speech regions (the typical 3-10 s
       dialog utterance) are packed into shared 30 s decoder windows with
       brief silence pads between. Whisper sees them as distinct segments
       and emits cues with window-relative timestamps; we demultiplex
       back to original-audio timestamps via each window's region_map.
       Cuts iGPU compute 1.5-3× on dialog-heavy films vs. the previous
       one-region-per-chunk approach. Toggle via `stt_region_packing`.

    All three stages preserve cue timestamps as original-audio seconds.
    """
    import numpy as np
    import soundfile as sf

    check_cancel()
    model, processor = _model_and_processor(
        settings.whisper_model, settings.openvino_device, str(settings.cache_dir),
    )
    check_cancel()

    segment_seconds = max(60, int(settings.stt_audio_segment_seconds or 600))
    segment_samples = segment_seconds * _SAMPLE_RATE
    overlap_seconds = max(0, int(settings.stt_segment_overlap_seconds or 0))
    overlap_samples = overlap_seconds * _SAMPLE_RATE
    packing_enabled = bool(settings.stt_region_packing)

    # ── decoder kwargs (constant across windows) ─────────────────────────
    # Force language + task once so each window's generate() call uses
    # the same prompt, regardless of optimum-intel kwarg quirks.
    generate_kwargs: dict = {"return_timestamps": True}
    if language_hint:
        try:
            forced = processor.get_decoder_prompt_ids(
                language=language_hint, task="transcribe", no_timestamps=False,
            )
            generate_kwargs["forced_decoder_ids"] = forced
        except Exception as e:
            _log.warning(
                "could not build forced_decoder_ids for language=%r (%s); "
                "letting Whisper auto-detect", language_hint, e,
            )

    batch_size = _BATCH_BY_MODEL.get(settings.whisper_model, _DEFAULT_BATCH)

    # Per-run aggregators for the .stats.json sidecar. The VAD aggregator
    # absorbs every per-segment ``detect_speech`` call's output; packing
    # / whisper aggregators get poked from inside the inner loop. None
    # of these access the heavy ML deps so they're cheap.
    from app import pipeline_metrics as pm_mod
    vad_agg = pm_mod.VadAggregator()
    packing_agg = pm_mod.PackingAggregator(enabled=packing_enabled)
    whisper_agg = pm_mod.WhisperAggregator()

    cues: list[Cue] = []
    next_id = 0
    detected_lang: str | None = None

    with sf.SoundFile(str(audio_path)) as snd:
        if snd.samplerate != _SAMPLE_RATE:
            raise RuntimeError(
                f"expected {_SAMPLE_RATE} Hz audio, got {snd.samplerate} Hz"
            )
        total_frames = snd.frames
        total_audio_seconds = total_frames / _SAMPLE_RATE
        _log.info(
            "STT streaming: %.1fs total audio | segment=%ds overlap=%ds packing=%s",
            total_audio_seconds, segment_seconds, overlap_seconds, packing_enabled,
        )

        file_pos = 0   # next read position in samples (absolute, in source audio)
        segment_idx = 0
        # Approximate progress denominator. We use ceil(total / segment) as
        # an upper bound; if cross-segment merging makes us consume more
        # than segment_samples per iteration we'll converge faster than the
        # bar suggests, which is fine.
        n_segments_est = max(1, math.ceil(total_frames / segment_samples))

        while file_pos < total_frames:
            check_cancel()

            # Seek to the next un-processed position. snd.seek + read keeps
            # us in O(1) per segment with no buffer-in-memory growth.
            snd.seek(file_pos)
            seg_audio = snd.read(
                frames=segment_samples + overlap_samples, dtype="float32",
            )
            if seg_audio.size == 0:
                break
            seg_offset_seconds = file_pos / _SAMPLE_RATE
            seg_length_samples = len(seg_audio)
            # The "main" portion is the first segment_samples of this read.
            # The trailing overlap_samples is there so a region straddling
            # the boundary can be captured fully without a re-read on the
            # next iteration.
            main_end_in_seg = min(segment_samples, seg_length_samples)

            # ── VAD over the segment + overlap window ───────────────────
            if settings.vad_enabled:
                speech_regions = detect_speech(seg_audio, _SAMPLE_RATE)
            else:
                speech_regions = [(0, seg_length_samples)]
            # vad_agg.observe is invoked AFTER consumed_samples /
            # processable_regions are computed (further down), so it
            # records only what THIS iteration consumes — not the
            # overlap zone that the next iteration will re-detect.

            # Decide where this segment ends and the next one starts:
            #   - Process all regions that BEGIN before main_end_in_seg.
            #   - For regions that straddle main_end_in_seg (start < main_end
            #     <= end), extend the consumption to the region's end so the
            #     full utterance is decoded here.
            #   - Regions that start at-or-after main_end_in_seg are deferred
            #     to the next iteration (next file_pos lands at their start
            #     via overlap reuse).
            consumed_samples = main_end_in_seg
            processable_regions: list[tuple[int, int]] = []
            for r_start, r_end in speech_regions:
                if r_start >= main_end_in_seg:
                    # Entirely in the overlap zone — defer to next segment.
                    break
                # Region starts before main_end_in_seg.
                processable_regions.append((r_start, r_end))
                if r_end > consumed_samples:
                    consumed_samples = r_end

            # Cap consumed_samples at the read length — we don't have data
            # past it. If a region's end was truncated by the read window,
            # the next iteration will see what's left of it (the file_pos
            # advance below stops at consumed_samples, so the region's
            # leftover sits at the very start of the next read).
            consumed_samples = min(consumed_samples, seg_length_samples)

            # The last segment of the file (file_pos + consumed_samples >=
            # total_frames) needs to also process regions that start in
            # the overlap zone — they're not "deferred" because there's
            # nowhere to defer them TO. Re-add anything we skipped.
            at_eof = (file_pos + consumed_samples) >= total_frames
            if at_eof:
                for r_start, r_end in speech_regions:
                    if (r_start, r_end) in processable_regions:
                        continue
                    processable_regions.append((r_start, r_end))
                consumed_samples = seg_length_samples

            # Record this iteration's VAD output into the aggregator
            # using consumed_samples as the audio denominator. Region
            # accounting is on processable_regions so deferred regions
            # don't get counted twice when the next iteration picks
            # them up via the overlap re-read.
            vad_agg.observe(
                seg_audio_seconds=consumed_samples / _SAMPLE_RATE,
                regions=processable_regions,
                sample_rate=_SAMPLE_RATE,
            )

            if not processable_regions:
                # All-silence segment. Advance and continue.
                file_pos += consumed_samples
                segment_idx += 1
                progress(min(1.0, file_pos / total_frames))
                continue

            speech_seconds_in_seg = sum(
                e - s for s, e in processable_regions
            ) / _SAMPLE_RATE
            _log.info(
                "STT segment %d (offset=%.1fs): %d region(s), %.1fs speech of "
                "%.1fs consumed",
                segment_idx + 1, seg_offset_seconds,
                len(processable_regions), speech_seconds_in_seg,
                consumed_samples / _SAMPLE_RATE,
            )

            # ── Plan windows ─────────────────────────────────────────────
            if packing_enabled:
                windows = plan_packed_windows(
                    processable_regions, _CHUNK_SAMPLES,
                    max_regions_per_window=int(settings.stt_max_regions_per_window or 0),
                )
            else:
                # Backward-compatible single-region-per-window mode (the
                # escape hatch for users who hit a region-packing
                # regression on a specific film).
                windows = _windows_from_simple_chunks(
                    processable_regions, _CHUNK_SAMPLES,
                )

            n_windows = len(windows)
            if n_windows == 0:
                file_pos += consumed_samples
                segment_idx += 1
                progress(min(1.0, file_pos / total_frames))
                continue

            for batch_start in range(0, n_windows, batch_size):
                check_cancel()
                batch_end = min(batch_start + batch_size, n_windows)
                batch = windows[batch_start:batch_end]

                batch_audio: list = []
                for win in batch:
                    batch_audio.append(_build_window_audio(seg_audio, win, np))

                features = processor.feature_extractor(
                    batch_audio, sampling_rate=_SAMPLE_RATE, return_tensors="pt",
                )
                token_ids = model.generate(features.input_features, **generate_kwargs)

                for k, win in enumerate(batch):
                    decoded = processor.tokenizer.decode(
                        token_ids[k], skip_special_tokens=False,
                        decode_with_timestamps=True,
                    )
                    # Window-level stats: how many regions Whisper saw
                    # packed into this 30 s decode. Single-region
                    # windows can't suffer pad-drop; only multi-region
                    # ones can. Both counts get aggregated.
                    packing_agg.record_window(n_regions=len(win.region_map))
                    # Parse with offset=0 to get window-relative cues; we
                    # remap each cue through the window's region_map below.
                    # remap_cue_to_original returns segment-relative seconds
                    # (the region_map's `original_start_samples` is in the
                    # segment's frame because VAD ran over seg_audio). We
                    # add seg_offset_seconds to lift them into source-audio
                    # coordinates — without this, every segment's cues end
                    # up stamped 0-segment_seconds and collapse onto the
                    # first segment's window of the timeline (regression
                    # introduced when the packing-based remap replaced the
                    # old additive offset path).
                    for win_start, win_end, text in _parse_segments(
                        decoded, 0.0,
                        on_drop=whisper_agg.record_degenerate_timestamp_drop,
                    ):
                        mapped = remap_cue_to_original(
                            win_start, win_end, win.region_map, _SAMPLE_RATE,
                        )
                        if mapped is None:
                            # Genuinely unmappable — empty region_map or
                            # a degenerate cue. Truly drop it.
                            packing_agg.record_cue_drop_pad_zone()
                            continue
                        orig_start, orig_end, was_snapped = mapped
                        if was_snapped:
                            # Pad-zone cue rescued via snap. The content
                            # is real (Whisper just mis-timed it by ≤ 0.5 s);
                            # we keep the cue but tag the recovery so the
                            # stats page can surface how many were affected.
                            packing_agg.record_cue_snap_pad_zone()
                        else:
                            packing_agg.record_cue_keep()
                        cues.append(Cue(
                            id=next_id,
                            start=orig_start + seg_offset_seconds,
                            end=orig_end + seg_offset_seconds,
                            text=text,
                        ))
                        next_id += 1
                    if detected_lang is None:
                        m = _LANG_TOKEN_RE.search(decoded)
                        if m:
                            detected_lang = m.group(1)

                # Smooth progress: anchor at file_pos, advance fractionally
                # within this segment's consumption.
                intra_frac = batch_end / n_windows
                progress_pos = file_pos + intra_frac * consumed_samples
                progress(min(1.0, progress_pos / total_frames))

            del seg_audio
            file_pos += consumed_samples
            segment_idx += 1

    progress(1.0)
    return TranscriptionResult(
        detected_language=language_hint or detected_lang or "en",
        cues=cues,
        pipeline_metrics=pm_mod.PipelineMetrics(
            vad=vad_agg.finalize(),
            packing=packing_agg.finalize(),
            whisper=whisper_agg.finalize(),
        ),
    )


def _build_window_audio(seg_audio, win: Window, np_mod) -> "np.ndarray":
    """Concatenate the audio slices for a window, with silence pads
    between, and zero-pad the tail to exactly _CHUNK_SAMPLES so Whisper's
    feature extractor (which is strict on length) accepts it."""
    parts: list = []
    for i, (s, e) in enumerate(win.audio_slices):
        if i > 0 and win.pad_samples_between > 0:
            parts.append(np_mod.zeros(win.pad_samples_between, dtype=np_mod.float32))
        parts.append(seg_audio[s:e])
    arr = np_mod.concatenate(parts) if len(parts) > 1 else parts[0]
    if len(arr) < _CHUNK_SAMPLES:
        arr = np_mod.pad(arr, (0, _CHUNK_SAMPLES - len(arr)))
    elif len(arr) > _CHUNK_SAMPLES:
        arr = arr[:_CHUNK_SAMPLES]
    return arr


def _windows_from_simple_chunks(
    speech_regions: list[tuple[int, int]],
    chunk_samples: int,
) -> list[Window]:
    """Fallback planner used when settings.stt_region_packing is False.
    Each speech region becomes one or more Windows of size chunk_samples
    (zero-padded if shorter). Single-region windows have a trivial
    region_map identical to the old plan_chunks output, so cue timestamps
    flow through remap_cue_to_original cleanly."""
    out: list[Window] = []
    for r_start, r_end in speech_regions:
        if r_end <= r_start:
            continue
        j = 0
        while j * chunk_samples < (r_end - r_start):
            slice_start = r_start + j * chunk_samples
            slice_end = min(r_end, slice_start + chunk_samples)
            out.append(Window(
                audio_slices=[(slice_start, slice_end)],
                pad_samples_between=0,
                region_map=[RegionEntry(
                    window_offset_samples=0,
                    original_start_samples=slice_start,
                    length_samples=slice_end - slice_start,
                )],
            ))
            j += 1
    return out
