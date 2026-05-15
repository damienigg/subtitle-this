"""Confidence-gated re-transcription pass for the audio mode pipeline.

The first STT pass uses balanced params (beam=5, int8). On hard audio
(score-bedded dialog, whispers, accented speech) some regions come out
weak — either no cues at all (Whisper's no-speech gate fired) or cues
with very low ``avg_logprob``. This module:

1. Reads the first-pass cue list + audio duration.
2. Identifies WEAK BUCKETS — 10-min audio windows where coverage is
   anomalously low OR average cue logprob is below the OpenAI Whisper
   re-decode threshold (-1.0).
3. For each weak bucket, extracts the corresponding audio range to a
   small temp WAV.
4. Re-runs STT with ``aggressive=True`` (beam=10, n-gram-repeat
   suppression, tighter log-prob threshold) on just that range.
5. Merges new cues back into the original list, replacing the cues
   in the affected range.

Safety mechanisms in place (in order of importance for OOM-resistance):

- **No double-load.** The aggressive re-pass re-uses the cached
  Whisper model from the first pass (faster_whisper.lru_cache
  hit). Peak RAM during re-pass is identical to the first pass —
  there is never a moment where two models are resident.

- **Bounded retry budget.** Total re-transcribed audio is capped at
  ``REFINE_MAX_AUDIO_FRACTION`` (20% of the source duration). If the
  weak-bucket analysis flags more than that, we re-pass only the
  worst buckets that fit in the budget. Hard ceiling on the time
  cost of the refine phase.

- **Early-out on clean audio.** If first-pass coverage is above
  ``REFINE_SKIP_COVERAGE_THRESHOLD`` (95%) AND average logprob is
  above the re-decode threshold across all buckets, the refine
  phase is a no-op. Most films skip the phase entirely; we only
  pay its cost when there's something to fix.

- **Single retry per bucket.** No recursion, no second-order retries.
  A weak bucket either gets re-decoded once or not at all.

- **Worse-result rejection.** If the aggressive re-pass produces
  FEWER cues for a bucket than the first pass had, we keep the
  first-pass result. The re-pass is meant to recover dropped
  dialog, not to lose it.

- **Cancel propagation.** ``check_cancel`` is called between
  bucket extractions and between aggressive transcribe calls, so
  the user can interrupt during refine the same way they can
  during the first pass.

Telemetry: counts of buckets evaluated, buckets re-decoded, cues
gained, audio seconds re-passed → folded into PipelineMetrics for
the stats page.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.config import settings
from app.pipeline import stt as stt_dispatcher
from app.pipeline.stt import Cue, TranscriptionResult


_log = logging.getLogger("subtitle_this")


# ── Tunables (constants, intentionally not Settings knobs) ───────────────────
# These exist as module-level constants rather than env-var-driven settings
# because the right values are properties of Whisper's behaviour, not
# user-preferential. Power users can patch them via monkeypatch in env-
# specific deployments.

#: Audio-coverage threshold above which the refine pass is skipped entirely.
#: 95% means: on a 2 h film, if at least 114 minutes have cues, we don't
#: bother refining. Most well-behaved films clear this.
REFINE_SKIP_COVERAGE_THRESHOLD = 0.95

#: Bucket size for the coverage analysis. 10 min is large enough that the
#: per-bucket cue density is statistically meaningful and small enough
#: that a single low-coverage bucket points to a specific scene.
BUCKET_SECONDS = 600.0

#: Audio fraction we'll re-pass before giving up. Caps the time/RAM cost
#: of the refine phase even on pathologically bad first-pass output.
REFINE_MAX_AUDIO_FRACTION = 0.20

#: Coverage threshold (cue display time / bucket duration) below which a
#: bucket is considered weak. 30% means: in this 10-min window, less than
#: 3 minutes of cues — anomalously low for typical dialog.
WEAK_BUCKET_COVERAGE_THRESHOLD = 0.30

#: Per-cue logprob threshold below which a cue is "low confidence".
#: Buckets whose mean cue logprob is below this are flagged weak.
#: -1.0 is the OpenAI Whisper-paper re-decode threshold.
WEAK_BUCKET_LOGPROB_THRESHOLD = -1.0


def _noop_progress(frac: float) -> None: ...
def _noop_cancel() -> None: ...


@dataclass
class RefineStats:
    """Telemetry for one refine pass. Folded into PipelineMetrics."""
    buckets_evaluated: int = 0
    buckets_weak: int = 0
    buckets_refined: int = 0
    cues_added: int = 0
    cues_replaced: int = 0
    audio_seconds_refined: float = 0.0
    skipped_reason: str | None = None     # populated when refine is a no-op


@dataclass
class _Bucket:
    """One coverage analysis window."""
    start: float
    end: float
    cue_count: int = 0
    display_seconds: float = 0.0
    mean_logprob: float | None = None    # None if no cues in this bucket
    cue_logprobs: list[float] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """Fraction of this bucket's duration that's covered by cues.
        0.0 = silent throughout (or VAD rejected everything),
        1.0 = continuously talking."""
        span = self.end - self.start
        if span <= 0:
            return 0.0
        return min(1.0, self.display_seconds / span)

    def is_weak(self) -> bool:
        """A bucket is weak when EITHER coverage is anomalously low OR
        the average cue confidence is below the re-decode threshold.
        Coverage low → Whisper rejected dialog as silence (the
        common case on score-bedded scenes). Logprob low → Whisper
        transcribed something but isn't sure what (the case on
        accented / whispered dialog)."""
        if self.coverage < WEAK_BUCKET_COVERAGE_THRESHOLD:
            return True
        if (self.mean_logprob is not None and
                self.mean_logprob < WEAK_BUCKET_LOGPROB_THRESHOLD):
            return True
        return False


def _build_buckets(cues: list[Cue], duration: float) -> list[_Bucket]:
    """Slice the audio duration into BUCKET_SECONDS windows and tally
    coverage + logprob per window."""
    if duration <= 0:
        return []
    n = max(1, int(duration // BUCKET_SECONDS) + (1 if duration % BUCKET_SECONDS else 0))
    buckets = [
        _Bucket(start=i * BUCKET_SECONDS, end=min((i + 1) * BUCKET_SECONDS, duration))
        for i in range(n)
    ]
    for cue in cues:
        # Cue can straddle a bucket boundary — count it in the bucket
        # where its midpoint falls. Good enough for coverage stats; the
        # alternative (proportional split) adds complexity for no
        # decision-quality gain.
        mid = (cue.start + cue.end) / 2.0
        idx = min(len(buckets) - 1, int(mid // BUCKET_SECONDS))
        b = buckets[idx]
        b.cue_count += 1
        b.display_seconds += max(0.0, cue.end - cue.start)
        if cue.avg_logprob is not None:
            b.cue_logprobs.append(cue.avg_logprob)
    for b in buckets:
        if b.cue_logprobs:
            b.mean_logprob = sum(b.cue_logprobs) / len(b.cue_logprobs)
    return buckets


def _select_buckets_within_budget(
    weak_buckets: list[_Bucket], duration: float,
) -> list[_Bucket]:
    """Pick the worst-coverage subset of weak buckets that fits within
    the audio-refine budget. Worst-first because those are the ones
    with the most to gain from re-decoding."""
    budget_seconds = duration * REFINE_MAX_AUDIO_FRACTION
    # Sort by coverage ASCENDING (worst first), then by mean_logprob
    # ascending as a tiebreaker (lower confidence first).
    ordered = sorted(
        weak_buckets,
        key=lambda b: (b.coverage, b.mean_logprob if b.mean_logprob is not None else 0.0),
    )
    chosen: list[_Bucket] = []
    used = 0.0
    for b in ordered:
        span = b.end - b.start
        if used + span > budget_seconds:
            break
        chosen.append(b)
        used += span
    return chosen


def _extract_audio_range(
    media_path: str, track_index: int, start: float, end: float,
) -> Path:
    """Slice the original audio track to a 16 kHz mono WAV covering
    just ``[start, end]``. Uses ffmpeg fast input-seek (``-ss`` before
    ``-i``) for ~ms-level precision at zero decode cost — accurate
    enough for re-passing a 10-min bucket. The result lives under
    cache_dir/tmp/ and is cleaned up by the caller."""
    tmp_dir = Path(settings.cache_dir) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=".refine.wav", delete=False, dir=str(tmp_dir),
    ) as tmp:
        out = Path(tmp.name)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-ss", f"{start:.3f}",
            "-i", media_path,
            "-map", f"0:{track_index}",
            "-t", f"{(end - start):.3f}",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            # No loudnorm here — the first-pass audio was already
            # normalised at extract time; re-applying would push it
            # further from the original tail-end distribution Whisper
            # saw on the first pass. We want the SAME signal, just
            # re-decoded with aggressive params.
            str(out),
        ],
        check=True,
        timeout=600,
    )
    return out


def refine_weak_buckets(
    transcription: TranscriptionResult,
    media_path: str,
    track_index: int,
    audio_duration_seconds: float,
    *,
    language_hint: str | None = None,
    progress: Callable[[float], None] = _noop_progress,
    check_cancel: Callable[[], None] = _noop_cancel,
) -> tuple[TranscriptionResult, RefineStats]:
    """Apply the confidence-gated re-pass to ``transcription`` and
    return the augmented result + stats.

    The original ``transcription`` is mutated in-place AND returned
    (callers can use either reference). Cues are re-numbered to stay
    contiguous after the merge."""
    stats = RefineStats()
    cues = transcription.cues

    # Early-out 1: no cues at all → nothing to refine against, skip.
    # (NoSpeech detection lives one level up; this is just defensive.)
    if not cues:
        stats.skipped_reason = "no_cues"
        return transcription, stats

    # Early-out 2: backend doesn't expose avg_logprob (OpenVINO path)
    # → we can't make confidence-based decisions. Fall back to coverage-
    # only mode? For now, skip entirely — the OpenVINO backend already
    # uses Silero VAD which is its own quality-protection layer.
    if all(c.avg_logprob is None for c in cues):
        stats.skipped_reason = "no_logprob_data"
        _log.info(
            "refine pass skipped: this backend doesn't expose per-cue "
            "logprob (typical for OpenVINO). The Silero-VAD pre-filter "
            "is the quality net on that path."
        )
        return transcription, stats

    buckets = _build_buckets(cues, audio_duration_seconds)
    stats.buckets_evaluated = len(buckets)

    # Early-out 3: globally clean audio. If overall coverage is high
    # AND no bucket is weak, the refine phase is pure overhead — bail.
    total_display = sum(b.display_seconds for b in buckets)
    overall_coverage = total_display / audio_duration_seconds if audio_duration_seconds > 0 else 0.0
    weak = [b for b in buckets if b.is_weak()]
    stats.buckets_weak = len(weak)
    if overall_coverage >= REFINE_SKIP_COVERAGE_THRESHOLD and not weak:
        stats.skipped_reason = "first_pass_clean"
        _log.info(
            "refine pass skipped: first-pass coverage %.1f%% with no weak "
            "buckets — the audio is clean, no re-decode needed.",
            overall_coverage * 100,
        )
        return transcription, stats

    # Pick the worst buckets that fit in the audio-refine budget.
    to_refine = _select_buckets_within_budget(weak, audio_duration_seconds)
    if not to_refine:
        stats.skipped_reason = "no_buckets_in_budget"
        return transcription, stats

    # Mutable list we'll rebuild in place.
    new_cue_list: list[Cue] = list(cues)
    total_refined_seconds = sum(b.end - b.start for b in to_refine)

    _log.info(
        "refine pass: %d weak bucket(s) flagged (%.1f s of audio = %.1f%% "
        "of total). %d selected within %.0f%% budget.",
        len(weak), sum(b.end - b.start for b in weak),
        100 * sum(b.end - b.start for b in weak) / audio_duration_seconds,
        len(to_refine), 100 * REFINE_MAX_AUDIO_FRACTION,
    )

    for idx, bucket in enumerate(to_refine):
        check_cancel()
        progress((idx + 0.1) / len(to_refine))

        # Extract just this range of the source audio.
        try:
            range_wav = _extract_audio_range(
                media_path, track_index, bucket.start, bucket.end,
            )
        except subprocess.CalledProcessError as e:
            _log.warning(
                "refine: ffmpeg failed extracting bucket %.1f-%.1fs (%s); "
                "skipping this bucket.", bucket.start, bucket.end, e,
            )
            continue

        try:
            check_cancel()
            # Re-decode with aggressive params. Uses the SAME cached
            # Whisper model that the first pass loaded — there is
            # never a moment with two models resident. The aggressive
            # flag is plumbed to faster-whisper; OpenVINO ignores it
            # (and we already early-out'd if avg_logprob was absent,
            # which excludes the OpenVINO path).
            re_result = stt_dispatcher.transcribe(
                range_wav,
                language_hint=language_hint or transcription.detected_language,
                check_cancel=check_cancel,
                aggressive=True,
            )
        finally:
            range_wav.unlink(missing_ok=True)

        # Re-pass cue timestamps are RELATIVE to range_wav start (0..).
        # Offset them back to absolute audio timestamps.
        new_in_range: list[Cue] = []
        for c in re_result.cues:
            new_in_range.append(Cue(
                id=-1,   # temp; renumbered after merge
                start=c.start + bucket.start,
                end=c.end + bucket.start,
                text=c.text,
                words=c.words,
                avg_logprob=c.avg_logprob,
            ))

        # Safety: if the aggressive pass produced FEWER cues, keep the
        # first-pass result for this bucket. The re-pass is supposed
        # to recover dropped dialog, not lose it.
        if len(new_in_range) < bucket.cue_count:
            _log.info(
                "refine: bucket %.1f-%.1fs got %d cues from aggressive "
                "re-pass vs %d on first pass — keeping first-pass result.",
                bucket.start, bucket.end, len(new_in_range), bucket.cue_count,
            )
            continue

        # Replace the cues that fall inside the bucket with the new ones.
        before = [c for c in new_cue_list if (c.start + c.end) / 2.0 < bucket.start]
        after = [c for c in new_cue_list if (c.start + c.end) / 2.0 >= bucket.end]
        new_cue_list = before + new_in_range + after

        stats.buckets_refined += 1
        stats.cues_added += max(0, len(new_in_range) - bucket.cue_count)
        stats.cues_replaced += min(bucket.cue_count, len(new_in_range))
        stats.audio_seconds_refined += bucket.end - bucket.start

    # Renumber the merged list so cue ids stay contiguous. Downstream
    # code (cache key, polish, translate) assumes 0..N-1 with no gaps.
    transcription.cues = [
        Cue(
            id=i,
            start=c.start,
            end=c.end,
            text=c.text,
            words=c.words,
            avg_logprob=c.avg_logprob,
        )
        for i, c in enumerate(new_cue_list)
    ]
    progress(1.0)

    if stats.buckets_refined:
        _log.info(
            "refine done: %d/%d weak buckets refined, +%d cues, %d cues "
            "replaced, %.1fs of audio re-passed.",
            stats.buckets_refined, stats.buckets_weak,
            stats.cues_added, stats.cues_replaced,
            stats.audio_seconds_refined,
        )
    return transcription, stats
