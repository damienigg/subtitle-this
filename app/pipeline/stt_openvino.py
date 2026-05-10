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
from app.pipeline.stt import Cue, TranscriptionResult
from app.pipeline.vad import detect_speech, plan_chunks


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


# Whisper auto-emits language tokens like <|en|>, <|fr|>, <|ja|> at the start
# of the decoded output when no language is forced. This set lets us pick the
# real language token out without false-positive matching on control tokens
# like <|transcribe|> or <|notimestamps|>.
_LANG_TOKEN_RE = re.compile(
    r"<\|(en|fr|es|de|it|pt|ja|ko|zh|ru|ar|hi|tr|vi|th|pl|nl|sv|no|da|"
    r"fi|cs|el|he|hu|ro|uk|id|ms|tl|ca|bn|fa|ur|tg|sk|sl|et|lv|lt|bg|mk|"
    r"sr|hr|bs|sq|az|ka|hy|kk|ky|uz|mn|my|km|lo|am|ti|so|sw|yo|ig|ha)\|>"
)


def _parse_segments(decoded: str, time_offset_s: float) -> list[tuple[float, float, str]]:
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

    Audio is processed in N-second SEGMENTS rather than as one big in-RAM
    buffer (see `settings.stt_audio_segment_seconds`). This keeps peak
    audio RAM bounded — for a 2 h film with the default 600 s segment
    size, ~75 MB of float32 audio is resident at any moment instead of
    ~500 MB. Each segment is independently VAD-filtered and chunked, then
    discarded before the next segment is read.

    The trade-off is that words straddling a segment boundary may split
    into two cues (one per segment). With the default 600 s segments and
    typical film durations, that's at most ~10 boundaries — acceptable for
    v1; tunable via `BABEL_STT_AUDIO_SEGMENT_SECONDS` if needed.
    """
    import numpy as np
    import soundfile as sf

    check_cancel()
    model, processor = _model_and_processor(
        settings.whisper_model, settings.openvino_device, str(settings.cache_dir),
    )
    check_cancel()

    # Open the file once, read the metadata, then stream segment-by-segment
    # below. We do NOT slurp the full audio into RAM here — that's the
    # whole point of the streaming refactor.
    with sf.SoundFile(str(audio_path)) as snd:
        if snd.samplerate != _SAMPLE_RATE:
            raise RuntimeError(
                f"expected {_SAMPLE_RATE} Hz audio, got {snd.samplerate} Hz"
            )
        total_frames = snd.frames
        total_audio_seconds = total_frames / _SAMPLE_RATE

        segment_seconds = max(60, int(settings.stt_audio_segment_seconds or 600))
        segment_samples = segment_seconds * _SAMPLE_RATE
        n_segments = max(1, math.ceil(total_frames / segment_samples))
        _log.info(
            "STT segmentation: %.1fs total audio → %d segment(s) of up to %ds",
            total_audio_seconds, n_segments, segment_seconds,
        )

        # ── decoder kwargs (constant across segments) ─────────────────────
        # Force language + task once so each segment's generate() call uses
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

        cues: list[Cue] = []
        next_id = 0
        detected_lang: str | None = None
        # Total chunks across segments (denominator for progress reporting).
        # We don't know this upfront — we report progress as
        # (segment_index + intra_segment_fraction) / n_segments instead, so
        # the bar advances smoothly across the whole transcription pass.

        for segment_idx in range(n_segments):
            check_cancel()

            # snd.read advances the file pointer; we read up to
            # segment_samples and let soundfile return whatever's left at
            # EOF. dtype=float32 avoids a second copy from int16.
            seg_audio = snd.read(frames=segment_samples, dtype="float32")
            if seg_audio.size == 0:
                # Defensive — shouldn't happen given total_frames, but if
                # the file is shorter than reported (mismatched header),
                # bail out cleanly.
                break
            seg_offset_seconds = (segment_idx * segment_samples) / _SAMPLE_RATE

            # ── VAD over THIS SEGMENT only ──────────────────────────────
            # Keeps the torch tensor allocation bounded to one segment of
            # audio (~75 MB at 600 s). Without VAD we still segment, just
            # treating the whole segment as one big speech region.
            if settings.vad_enabled:
                seg_speech_regions = detect_speech(seg_audio, _SAMPLE_RATE)
                if not seg_speech_regions:
                    progress((segment_idx + 1) / n_segments)
                    continue
            else:
                seg_speech_regions = [(0, len(seg_audio))]

            seg_chunks = plan_chunks(seg_speech_regions, _CHUNK_SAMPLES, _SAMPLE_RATE)
            n_seg_chunks = len(seg_chunks)
            if n_seg_chunks == 0:
                progress((segment_idx + 1) / n_segments)
                continue
            speech_seconds = sum(e - s for s, e in seg_speech_regions) / _SAMPLE_RATE
            seg_audio_seconds = len(seg_audio) / _SAMPLE_RATE
            _log.info(
                "STT segment %d/%d: %d speech regions, %.1fs of %.1fs (%d%%); %d chunks",
                segment_idx + 1, n_segments,
                len(seg_speech_regions), speech_seconds, seg_audio_seconds,
                round(100 * speech_seconds / max(seg_audio_seconds, 1e-9)), n_seg_chunks,
            )

            for batch_start in range(0, n_seg_chunks, batch_size):
                check_cancel()
                batch_end = min(batch_start + batch_size, n_seg_chunks)
                batch = seg_chunks[batch_start:batch_end]

                # Each chunk is exactly 30s of float32 audio. Region tails
                # shorter than 30s are zero-padded — VAD has already trimmed
                # surrounding silence so the speech anchors the decoder;
                # the pad has no semantic content.
                batch_chunks: list = []
                for chunk in batch:
                    audio_slice = seg_audio[chunk.start_sample:chunk.end_sample]
                    if len(audio_slice) < _CHUNK_SAMPLES:
                        audio_slice = np.pad(
                            audio_slice, (0, _CHUNK_SAMPLES - len(audio_slice))
                        )
                    batch_chunks.append(audio_slice)

                features = processor.feature_extractor(
                    batch_chunks, sampling_rate=_SAMPLE_RATE, return_tensors="pt",
                )
                token_ids = model.generate(features.input_features, **generate_kwargs)

                for k, chunk in enumerate(batch):
                    decoded = processor.tokenizer.decode(
                        token_ids[k], skip_special_tokens=False,
                        decode_with_timestamps=True,
                    )
                    # chunk.orig_offset_s is segment-relative; add the
                    # segment's own offset to get original-audio-relative
                    # cue timestamps.
                    global_offset = chunk.orig_offset_s + seg_offset_seconds
                    for start, end, text in _parse_segments(decoded, global_offset):
                        cues.append(Cue(id=next_id, start=start, end=end, text=text))
                        next_id += 1
                    if detected_lang is None:
                        m = _LANG_TOKEN_RE.search(decoded)
                        if m:
                            detected_lang = m.group(1)

                # Smooth progress: position within this segment + segment index.
                intra = batch_end / n_seg_chunks
                progress((segment_idx + intra) / n_segments)

            # Free the segment buffer before reading the next one. CPython
            # reference counting drops it as soon as we rebind on the next
            # iteration, but explicit deletion makes the intent clear and
            # avoids any short-lived overlap when the next read allocates.
            del seg_audio

    return TranscriptionResult(
        detected_language=language_hint or detected_lang or "en",
        cues=cues,
    )
