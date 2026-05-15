"""STT dispatcher. Concrete backends live in sibling modules and are loaded lazily
so we never import a backend's heavy deps unless it's actually selected."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def try_malloc_trim() -> None:
    """Force glibc to return unused malloc arenas back to the kernel.

    Without this, after a large model (Whisper, NLLB) is freed by
    Python's GC, the memory often stays in the process's heap held by
    glibc's internal arenas instead of returning to the OS. The cgroup
    still sees it as 'in use', and the next big allocation (e.g. NLLB
    loading right after Whisper release) can trip the OOM-killer at
    a moment when the application has *logically* freed the previous
    model — that was the failure pattern that produced the 1.96 GB
    anon-rss OOM on TrueNAS (the kill fired before the new allocation
    completed, but cgroup memory was already capped by the un-trimmed
    arenas from the previous phase).

    Linux/glibc only — silent no-op on Alpine/musl or non-Linux hosts.
    Our shipped container images (Debian python:3.12-slim and
    Ubuntu24-based openvino runtime) both use glibc, so this is
    effective there.
    """
    import ctypes
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except (OSError, AttributeError):
        pass


@dataclass
class Word:
    """One word from a faster-whisper word-timestamps decode.

    - ``start`` / ``end``: audio-anchored timestamps, frame-accurate
      via Whisper's cross-attention DTW (±100 ms vs. ±300 ms for
      chunk-level timing).
    - ``probability``: the model's confidence in this word, 0..1.
      Aggregated to ``Cue.avg_logprob`` for the confidence-gated
      re-transcription pass — segments with consistently low word
      probabilities are the ones the 0.8.0 retry-pass attacks.

    Only populated on the faster-whisper backend with
    ``word_timestamps=True``. The OpenVINO backend doesn't expose
    word-level DTW (optimum-intel doesn't surface the cross-attention
    matrices), so cues from that backend have ``words=None``."""
    start: float
    end: float
    text: str
    probability: float


@dataclass
class Cue:
    id: int
    start: float
    end: float
    text: str
    # Word-level timestamps from Whisper's cross-attention DTW (when
    # the backend supports it). Stays None on the OpenVINO path and
    # on transcript-cache hits from pre-0.8.0 entries.
    words: list[Word] | None = None
    # Mean log-probability across the cue's tokens, as reported by
    # the decoder. Used by the confidence-gated re-transcription
    # pass to identify weak regions worth re-decoding with aggressive
    # params. Lower (more negative) = less confident. -1.0 is the
    # OpenAI Whisper-paper threshold for "this region needs another
    # look". None on backends that don't expose it.
    avg_logprob: float | None = None


@dataclass
class TranscriptionResult:
    detected_language: str
    cues: list[Cue]
    # Per-run instrumentation counters (VAD / packing / whisper). None
    # for the CPU backend and any older transcript-cache entry that
    # was stored before the field existed — downstream consumers must
    # tolerate the absence. PipelineMetrics annotation resolves lazily
    # thanks to ``from __future__ import annotations`` at the top of
    # this module, so the import doesn't have to live at module load.
    pipeline_metrics: PipelineMetrics | None = None


from app.pipeline_metrics import PipelineMetrics   # noqa: E402,F401


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
    """``progress`` reports fractional completion in [0,1] within
    transcription (the outer pipeline maps it onto its own 0-100
    budget). ``check_cancel`` raises JobCanceled if the user has
    clicked cancel — backends call it between segments / chunks so
    cancel takes effect within seconds, not minutes.

    ``aggressive=True`` (only meaningful on the cpu/faster-whisper
    backend) enables the confidence-gated re-pass mode: larger beam,
    n-gram-repeat suppression, tighter log-prob threshold. Used by
    ``stt_refine.refine_weak_buckets`` to re-decode weak regions.
    The OpenVINO backend ignores the flag (its decode path doesn't
    expose those knobs — optimum-intel wraps the model differently)."""
    from app.config import settings
    backend = settings.whisper_backend.lower()
    if backend == "openvino":
        from app.pipeline.stt_openvino import transcribe as run
        # OpenVINO ignores ``aggressive`` — its decode path doesn't
        # expose the relevant knobs.
        return run(audio_path, language_hint=language_hint,
                   progress=progress, check_cancel=check_cancel)
    elif backend == "cpu":
        from app.pipeline.stt_faster_whisper import transcribe as run
        return run(audio_path, language_hint=language_hint,
                   progress=progress, check_cancel=check_cancel,
                   aggressive=aggressive)
    else:
        raise ValueError(
            f"Unknown BABEL_WHISPER_BACKEND={settings.whisper_backend!r} (expected 'cpu' or 'openvino')"
        )


def release() -> None:
    """Evict the active backend's cached Whisper model. Dispatcher mirror of
    transcribe().

    Called by processor.py between the STT and translation phases so the
    ~1-1.5 GB Whisper weights don't sit resident while NLLB / vision-LLM
    state loads — the two together exceed the default 6 GB cgroup limit on
    typical NAS deployments and trigger a silent kernel OOM-kill at the
    80% mark of the pipeline. Reloading on the next job costs ~10-30s,
    which is dwarfed by the transcription cost itself.

    Safe to call when no model is cached — cache_clear() is a no-op then.
    """
    from app.config import settings
    backend = settings.whisper_backend.lower()
    if backend == "openvino":
        from app.pipeline.stt_openvino import release_model
    elif backend == "cpu":
        from app.pipeline.stt_faster_whisper import release_model
    else:
        return
    release_model()
