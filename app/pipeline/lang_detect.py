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
        # beam_size=1 + condition_on_previous_text=False = the cheapest pass.
        # We don't care about the transcribed text, only info.language.
        segments, info = model.transcribe(
            sample,
            language=None,
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=True,
        )
        # faster-whisper's `segments` is a lazy generator — Whisper's
        # language detection runs on the FIRST decoder step, so we only
        # need to advance the iterator once for `info.language` to be
        # populated. We don't need (and would pay for) draining it.
        # On a completely silent sample the generator yields zero
        # segments and info.language stays as Whisper's pre-detection
        # default — the final `info.language or None` returns None in
        # that case, which is the correct signal to upstream.
        next(iter(segments), None)
    except Exception:
        return None

    return info.language or None
