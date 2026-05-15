from functools import lru_cache
from pathlib import Path
from typing import Callable

from faster_whisper import WhisperModel

from app.config import settings
from app.pipeline.stt import Cue, TranscriptionResult, Word


@lru_cache(maxsize=1)
def _model(name: str, device: str, compute_type: str) -> WhisperModel:
    """Cache keyed by config so settings changes (UI or env) reload the model.
    maxsize=1 — toggling whisper_model in the UI evicts the previous one
    rather than keeping both resident. Whisper-large weights are ~3 GB;
    holding a spare doubles RAM for no real workflow benefit."""
    return WhisperModel(name, device=device, compute_type=compute_type)


def release_model() -> None:
    """Evict the cached CPU Whisper model. Mirror of stt_openvino.release_model
    — called between transcribe and translate so the local NLLB / vision-LLM
    state can load without piling on top of an idle Whisper still resident.
    try_malloc_trim() returns the freed glibc arenas to the kernel; see
    its docstring in stt.py for why gc.collect() alone isn't enough."""
    import gc
    from app.pipeline.stt import try_malloc_trim
    _model.cache_clear()
    gc.collect()
    try_malloc_trim()


def _noop_progress(frac: float) -> None: ...
def _noop_cancel() -> None: ...


def transcribe(
    audio_path: Path,
    language_hint: str | None = None,
    *,
    progress: Callable[[float], None] = _noop_progress,
    check_cancel: Callable[[], None] = _noop_cancel,
    aggressive: bool = False,
) -> TranscriptionResult:
    """Transcribe ``audio_path`` and return cues with word-level
    timestamps + per-cue avg_logprob attached.

    ``aggressive=True`` enables the confidence-gated re-pass mode:
    beam_size=10 (vs. 5), no_repeat_ngram_size=3 to suppress stuck
    n-gram loops, tighter log_prob_threshold. Costs ~2× wall clock
    on the affected audio range; only used by stt_refine for the
    weak-bucket re-pass."""
    model = _model(settings.whisper_model, settings.whisper_device, settings.whisper_compute_type)
    transcribe_kwargs = dict(
        language=language_hint,
        vad_filter=True,
        beam_size=10 if aggressive else 5,
        # Word-level timestamps via Whisper's cross-attention DTW.
        # ~+20 % wall-clock on STT, ~50 MB transient buffer. Gives us
        # frame-accurate timing (±100 ms vs ±300 ms chunk-level) AND
        # per-word probability scores that the confidence-gated
        # re-transcription pass uses to find weak regions.
        word_timestamps=True,
        # ``condition_on_previous_text=False`` is the recommended
        # setting for long-form transcription per the Whisper paper
        # (Section 4.5) and faster-whisper's own README: with it
        # enabled (the library default), the model conditions each
        # 30 s window on the previous window's TEXT — which causes
        # cascading hallucinations after a silent gap (Whisper
        # generates "Thank you. Thanks for watching." repeatedly,
        # then conditions the next window on that, and the loop
        # continues for minutes). On dialog-heavy films with score-
        # bedded scenes, this is THE main source of nonsense cues.
        # Disabling it costs a bit of cross-window context but
        # eliminates the cascading-hallucination class entirely.
        condition_on_previous_text=False,
        # Filter out segments with very low average log-probability —
        # those are the model's "I'm not sure but here's a guess"
        # outputs, which on silence become exactly the signature
        # YouTube-style hallucinations we want to drop. -1.0 is the
        # OpenAI Whisper default; tighter on the aggressive re-pass.
        log_prob_threshold=-0.8 if aggressive else -1.0,
        # And drop segments where the no-speech probability is high —
        # Whisper's own gate against transcribing silence as if it
        # were speech. 0.6 is the OpenAI default.
        no_speech_threshold=0.6,
    )
    if aggressive:
        # Suppress stuck-loop n-gram repetitions ("yeah yeah yeah").
        # The anti-hallucination filter catches these post-decode, but
        # blocking them at decode time on the re-pass means the model
        # spends its second-chance compute on actual decode work, not
        # on generating tokens we're about to drop.
        transcribe_kwargs["no_repeat_ngram_size"] = 3

    segments, info = model.transcribe(str(audio_path), **transcribe_kwargs)
    # info.duration is the audio length in seconds (post-VAD when applicable).
    # Each yielded segment has .end (audio timestamp), so segment.end /
    # duration is a fair fractional progress estimate.
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    cues: list[Cue] = []
    for i, seg in enumerate(segments):
        check_cancel()
        text = seg.text.strip()
        if not text:
            if duration > 0:
                progress(float(seg.end) / duration)
            continue
        # Word-level data — present when word_timestamps=True. The
        # objects faster-whisper yields are namedtuple-like with
        # .start / .end / .word / .probability. We snapshot them into
        # our own Word dataclass so downstream code doesn't depend
        # on faster-whisper's internal types.
        words: list[Word] | None = None
        raw_words = getattr(seg, "words", None)
        if raw_words:
            words = [
                Word(
                    start=float(w.start),
                    end=float(w.end),
                    text=str(w.word),
                    probability=float(w.probability),
                )
                for w in raw_words
            ]
        avg_logprob = getattr(seg, "avg_logprob", None)
        if avg_logprob is not None:
            avg_logprob = float(avg_logprob)
        cues.append(Cue(
            id=i,
            start=float(seg.start),
            end=float(seg.end),
            text=text,
            words=words,
            avg_logprob=avg_logprob,
        ))
        if duration > 0:
            progress(float(seg.end) / duration)
    progress(1.0)
    return TranscriptionResult(detected_language=info.language, cues=cues)
