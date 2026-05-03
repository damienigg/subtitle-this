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

Trade-off: we do simple non-overlapping 30s chunking (no stride). Words
that straddle a chunk boundary may be split into two cues. Acceptable for
v1; worth revisiting only if users report visible artifacts.

Progress reporting is now true per-chunk i/N — no more cosmetic heartbeat,
no more RTF history needed for estimation. Cancel between chunks is
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


_log = logging.getLogger("subtitle_this")
_HF_PREFIX = "openai/whisper-"
_CHUNK_SECONDS = 30
_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = _CHUNK_SECONDS * _SAMPLE_RATE


def _noop_progress(frac: float) -> None: ...
def _noop_cancel() -> None: ...


@lru_cache(maxsize=2)
def _model_and_processor(model_name: str, device: str, cache_root: str):
    """Cache keyed by config so settings changes reload cleanly. Heavy
    imports stay inside so the CPU backend doesn't pay them at import time.

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
    """
    markers = list(re.finditer(r"<\|(\d+\.\d+)\|>", decoded))
    out: list[tuple[float, float, str]] = []
    for i in range(len(markers) - 1):
        m1, m2 = markers[i], markers[i + 1]
        start = float(m1.group(1)) + time_offset_s
        end = float(m2.group(1)) + time_offset_s
        text = decoded[m1.end():m2.start()].strip()
        if text and end > start:
            out.append((start, end, text))
    return out


def transcribe(
    audio_path: Path,
    language_hint: str | None = None,
    *,
    progress: Callable[[float], None] = _noop_progress,
    check_cancel: Callable[[], None] = _noop_cancel,
) -> TranscriptionResult:
    import numpy as np
    import soundfile as sf

    audio, sr = sf.read(str(audio_path))
    if sr != _SAMPLE_RATE:
        raise RuntimeError(f"expected {_SAMPLE_RATE} Hz audio, got {sr} Hz")

    check_cancel()
    model, processor = _model_and_processor(
        settings.whisper_model, settings.openvino_device, str(settings.cache_dir),
    )
    check_cancel()

    n_chunks = max(1, math.ceil(len(audio) / _CHUNK_SAMPLES))
    detected_lang: str | None = None
    cues: list[Cue] = []
    next_id = 0

    # The portable way to force language + task across transformers /
    # optimum-intel versions is forced_decoder_ids built by the processor,
    # NOT passing language=/task= as generate() kwargs (which are sometimes
    # rejected by OVModel.generate() depending on the optimum-intel build).
    # When language_hint is None, we leave forced_decoder_ids unset so
    # Whisper auto-detects each chunk.
    generate_kwargs: dict = {"return_timestamps": True}
    if language_hint:
        try:
            forced = processor.get_decoder_prompt_ids(
                language=language_hint, task="transcribe", no_timestamps=False,
            )
            generate_kwargs["forced_decoder_ids"] = forced
        except Exception as e:
            # If the processor rejects the language code (rare), let Whisper
            # auto-detect rather than failing the whole job. Log so we know.
            _log.warning(
                "could not build forced_decoder_ids for language=%r (%s); "
                "letting Whisper auto-detect", language_hint, e,
            )

    for i in range(n_chunks):
        check_cancel()
        start_sample = i * _CHUNK_SAMPLES
        end_sample = min(len(audio), start_sample + _CHUNK_SAMPLES)
        chunk = audio[start_sample:end_sample]
        # Whisper's mel feature extractor expects exactly 30s of audio AND
        # float32 dtype (soundfile returns float64 by default for WAV PCM,
        # which the feature extractor accepts but downstream OpenVINO ops
        # are strictly fp32). Cast eagerly + pad short final chunk with
        # zeros (silence — Whisper emits no segments for it).
        if chunk.dtype != np.float32:
            chunk = chunk.astype(np.float32)
        if len(chunk) < _CHUNK_SAMPLES:
            chunk = np.pad(chunk, (0, _CHUNK_SAMPLES - len(chunk)))

        features = processor.feature_extractor(
            chunk, sampling_rate=_SAMPLE_RATE, return_tensors="pt",
        )
        token_ids = model.generate(features.input_features, **generate_kwargs)
        decoded = processor.tokenizer.decode(
            token_ids[0], skip_special_tokens=False, decode_with_timestamps=True,
        )

        chunk_offset_s = float(i * _CHUNK_SECONDS)
        for start, end, text in _parse_segments(decoded, chunk_offset_s):
            cues.append(Cue(id=next_id, start=start, end=end, text=text))
            next_id += 1

        if detected_lang is None:
            m = _LANG_TOKEN_RE.search(decoded)
            if m:
                detected_lang = m.group(1)

        progress((i + 1) / n_chunks)

    return TranscriptionResult(
        detected_language=language_hint or detected_lang or "en",
        cues=cues,
    )
