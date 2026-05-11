"""Language detection pre-pass for the OpenVINO Whisper backend.

The OpenVINO HF pipeline doesn't surface Whisper's auto-detected language
(see app/pipeline/stt_openvino.py). When the source audio track is untagged,
that bug propagates downstream: NLLB and DeepL get told the wrong source
language and produce garbage. This module fixes that by running a quick
language-detection pass with `faster-whisper` (tiny model, ~75 MB on disk)
on the first ~30 seconds of the extracted WAV.

Cheap (2-3s on CPU after the model is warmed) and only triggered when the
ffprobe track tag is missing AND the configured Whisper backend is openvino.
The CPU backend (`faster-whisper`) does its own detection during the main
transcribe call, so we don't run this pre-pass there.
"""
from functools import lru_cache
from pathlib import Path


_DETECTOR_MODEL = "tiny"
_DETECTION_SECONDS = 30


@lru_cache(maxsize=1)
def _detector():
    """One process-wide tiny Whisper instance for language detection."""
    from faster_whisper import WhisperModel
    return WhisperModel(_DETECTOR_MODEL, device="cpu", compute_type="int8")


def release_detector() -> None:
    """Drop the cached tiny Whisper detector (~75 MB on disk, ~250 MB
    resident). Called by processor.py after the pre-pass succeeds — we
    don't need it again in this job, and the freed RAM is helpful on
    capped containers where every 100 MB counts at the translation peak.
    try_malloc_trim() returns the freed glibc arenas to the kernel; see
    its docstring in stt.py for why gc.collect() alone isn't enough."""
    import gc
    from app.pipeline.stt import try_malloc_trim
    _detector.cache_clear()
    gc.collect()
    try_malloc_trim()


def detect(wav_path: Path) -> str | None:
    """Run Whisper's language detection on the first ~30s of audio. Returns
    the ISO 639-1 code (e.g. 'fr', 'ja') or None if detection failed.

    Reads ONLY the first 30s of the wav (not the full file). The previous
    implementation called `sf.read(wav_path)` which loaded the entire 2h+
    audio buffer into RAM just to slice off 30s — wasteful enough on its
    own that it was one of two simultaneous full-wav allocations (the
    other in stt_openvino.transcribe) right before the heaviest stage.
    """
    try:
        import soundfile as sf
    except ImportError:
        return None

    try:
        with sf.SoundFile(str(wav_path)) as f:
            if f.samplerate != 16000:
                return None
            sample = f.read(frames=_DETECTION_SECONDS * f.samplerate, dtype="float32")
    except Exception:
        return None

    if len(sample) == 0:
        return None

    try:
        model = _detector()
        # `detect_language` runs ONLY the language-detection forward pass
        # (~50 ms on tiny/CPU), instead of the previous transcribe()+
        # advance-iterator dance which ran full decoder inference on the
        # 30s sample. ~3-5× faster end-to-end on the pre-pass with the
        # same accuracy. Returns (language, probability) — we only need
        # the code; the prob is informational.
        language, _prob = model.detect_language(sample)
    except Exception:
        return None

    return language or None
