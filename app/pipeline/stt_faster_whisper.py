from functools import lru_cache
from pathlib import Path
from typing import Callable

from faster_whisper import WhisperModel

from app.config import settings
from app.pipeline.stt import Cue, TranscriptionResult


@lru_cache(maxsize=2)
def _model(name: str, device: str, compute_type: str) -> WhisperModel:
    """Cache keyed by config so settings changes (UI or env) reload the model.
    maxsize=2 keeps one fallback warm when the user toggles between two models."""
    return WhisperModel(name, device=device, compute_type=compute_type)


def _noop_progress(frac: float) -> None: ...
def _noop_cancel() -> None: ...


def transcribe(
    audio_path: Path,
    language_hint: str | None = None,
    *,
    progress: Callable[[float], None] = _noop_progress,
    check_cancel: Callable[[], None] = _noop_cancel,
) -> TranscriptionResult:
    model = _model(settings.whisper_model, settings.whisper_device, settings.whisper_compute_type)
    segments, info = model.transcribe(
        str(audio_path),
        language=language_hint,
        vad_filter=True,
        beam_size=5,
    )
    # info.duration is the audio length in seconds (post-VAD when applicable).
    # Each yielded segment has .end (audio timestamp), so segment.end /
    # duration is a fair fractional progress estimate.
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    cues: list[Cue] = []
    for i, seg in enumerate(segments):
        check_cancel()
        text = seg.text.strip()
        if text:
            cues.append(Cue(id=i, start=float(seg.start), end=float(seg.end), text=text))
        if duration > 0:
            progress(float(seg.end) / duration)
    progress(1.0)
    return TranscriptionResult(detected_language=info.language, cues=cues)
