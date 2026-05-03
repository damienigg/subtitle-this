"""OpenVINO STT backend via optimum-intel.

Uses Hugging Face Whisper exported to OpenVINO IR. The first call for a given
model triggers download + IR conversion (slow, 5-30 min depending on size);
subsequent calls hit the cached IR and run on the configured device.
"""
import logging
from functools import lru_cache
from pathlib import Path
from typing import Callable

import soundfile as sf

from app.config import settings
from app.pipeline.openvino_introspect import log_selected_device
from app.pipeline.stt import Cue, TranscriptionResult


def _noop_progress(frac: float) -> None: ...
def _noop_cancel() -> None: ...


_log = logging.getLogger("subtitle_this")
_HF_PREFIX = "openai/whisper-"


@lru_cache(maxsize=2)
def _pipeline(model_name: str, device: str, cache_root: str):
    """Cache keyed by config so settings changes reload the pipeline.
    Heavy imports stay inside so the CPU backend doesn't pay them at import time."""
    from optimum.intel import OVModelForSpeechSeq2Seq
    from transformers import AutoProcessor, pipeline as hf_pipeline

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

    return hf_pipeline(
        "automatic-speech-recognition",
        model=model,
        tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        chunk_length_s=30,
        return_timestamps=True,
    )


def transcribe(
    audio_path: Path,
    language_hint: str | None = None,
    *,
    progress: Callable[[float], None] = _noop_progress,
    check_cancel: Callable[[], None] = _noop_cancel,
) -> TranscriptionResult:
    audio, sr = sf.read(str(audio_path))
    if sr != 16000:
        raise RuntimeError(f"expected 16 kHz audio, got {sr} Hz")

    check_cancel()
    pipe = _pipeline(settings.whisper_model, settings.openvino_device, str(settings.cache_dir))
    check_cancel()
    generate_kwargs: dict = {"task": "transcribe"}
    if language_hint:
        generate_kwargs["language"] = language_hint

    # The HF pipeline takes the entire audio array and chunks internally —
    # no per-chunk callback is exposed. Pretend the call linearly fills 0→1
    # by reporting a small bump up-front, the rest at the end. UI gets a
    # responsive bar at start + completion, with a long flat in between
    # (the real transcribe time).
    progress(0.05)
    result = pipe(audio, return_timestamps=True, generate_kwargs=generate_kwargs)
    check_cancel()
    progress(1.0)

    cues: list[Cue] = []
    for i, chunk in enumerate(result.get("chunks", [])):
        ts = chunk.get("timestamp") or (None, None)
        if ts[0] is None or ts[1] is None:
            continue
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        cues.append(Cue(id=i, start=float(ts[0]), end=float(ts[1]), text=text))

    # The HF pipeline doesn't surface the language detected by Whisper. Two
    # sources of truth for `language_hint` upstream:
    # 1. ffprobe track tag (when the file is properly tagged)
    # 2. faster-whisper-tiny language-detection pre-pass run by processor.py
    #    when the track has no tag (see app/pipeline/lang_detect.py)
    # The "en" fallback only triggers if BOTH the file is untagged AND the
    # pre-pass returned nothing (e.g. silent or extremely noisy first 30s).
    detected = language_hint or "en"
    return TranscriptionResult(detected_language=detected, cues=cues)
