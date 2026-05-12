"""Subtitle quality / coverage statistics.

When a film comes back with a "completed" status, the question the user
actually has is "did it produce a *good* result?" — and that's not
something the success/failure bit can answer. The Inception 0.7.2
regression is a good example: the run completed without an exception,
but the .vtt had every cue collapsed onto the first 10 minutes of the
timeline. "Completed" doesn't mean "correct".

This module computes a bag of objective metrics from a finished .vtt
that, taken together, let an operator spot pathologies a quick glance
at the file wouldn't catch:

- **Cue count** — too few vs. an expected dialog density on a 2 h film
  is a strong "missed dialog" signal (Whisper / VAD under-detection).
- **Duration distribution** — a heavy <0.5 s tail means Whisper is
  emitting compressed timestamps (a known artefact of region packing
  with discontinuity in the audio).
- **Per-10-min coverage buckets** — surfaces zone-by-zone gaps. A
  bucket at 10 % vs. its neighbors at 70 % flags a scene where VAD
  rejected most of the dialog. A trailing zero bucket means the .vtt
  was cut short (or, before 0.7.2, that every cue collapsed onto
  earlier buckets).
- **Character density** — combined with display time, lets the user
  sanity-check the WPM rate against human-readable subtitle norms
  (12-17 chars/s sustained is the SDH ceiling).

All metrics are computed from the .vtt text alone — no media probe, no
re-running the pipeline — so they're cheap to recompute on-demand for
any cached entry. A sidecar ``<file>.stats.json`` is also written next
to the .vtt at job completion so the file is portable: copy it off the
NAS and the numbers travel with it.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


# WebVTT timestamp: HH:MM:SS.mmm. The Whisper-tagged variant uses a dot
# before the millis (per spec). SRT uses comma but we only parse VTT here.
_TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _parse_cues(vtt_text: str) -> list[tuple[float, float, str]]:
    """Walk the .vtt and return [(start_s, end_s, text)]. NOTE blocks,
    WEBVTT preamble, and blank separators are skipped. Multi-line cue
    text is joined with a single space so character counts reflect the
    actual readable string, not the wrap layout."""
    cues: list[tuple[float, float, str]] = []
    blocks = vtt_text.split("\n\n")
    for block in blocks:
        lines = block.strip().split("\n")
        if not lines:
            continue
        # Find the timestamp line (first match wins — cue identifier
        # lines optionally precede it, but we don't need them).
        for i, line in enumerate(lines):
            m = _TS_RE.match(line.strip())
            if m:
                start_s = _to_seconds(*m.group(1, 2, 3, 4))
                end_s = _to_seconds(*m.group(5, 6, 7, 8))
                text = " ".join(lines[i + 1:]).strip()
                cues.append((start_s, end_s, text))
                break
    return cues


@dataclass
class DurationBuckets:
    """Cue display-duration histogram. Buckets sized to surface the
    pathologies the .vtt analyses actually call out:
    - <0.5 s: Whisper-compressed timestamps (region-packing artefact).
    - 0.5-1 s: short reactions / single words, plausible.
    - 1-2 s: typical short utterance.
    - 2-5 s: typical sentence.
    - >5 s: long line, often a chunked-cue artefact worth checking.
    """
    lt_0_5: int = 0
    lt_1_0: int = 0
    lt_2_0: int = 0
    lt_5_0: int = 0
    gte_5_0: int = 0


@dataclass
class CoverageBucket:
    """Per-10-minute slice of the timeline."""
    start_min: int
    end_min: int
    cue_count: int


@dataclass
class VttStats:
    """The full stats record written to the sidecar and rendered by the
    UI. Every field is JSON-safe (no datetimes — produced_at_epoch is
    a float UNIX time, formatted for display in the template)."""
    schema_version: str = "1"
    produced_at_epoch: float = 0.0

    # Source identifiers — let an unrelated viewer recognize what this
    # file describes without needing the surrounding directory tree.
    media_path: str | None = None
    media_name: str | None = None
    source_lang: str | None = None
    target_lang: str | None = None
    mode: str | None = None
    provider: str | None = None
    whisper_model: str | None = None
    detected_source_language: str | None = None
    cache_key: str | None = None

    # Job-level timings (only present when computed from a live job;
    # missing on on-demand recomputes from cached .vtt).
    took_seconds: float | None = None

    # Per-run pipeline telemetry — VAD coverage, packing pad-drops,
    # whisper degenerate-timestamp counts. None when the originating
    # run didn't instrument it (CPU backend, or a pre-0.7.6 cache).
    pipeline_metrics: dict[str, Any] | None = None

    # Heuristic quality score (0-100) + breakdown. Computed at
    # compute_from_vtt time from the same data — kept in the record
    # so the sidecar JSON carries it for downstream consumers without
    # them having to re-evaluate the rules.
    quality: dict[str, Any] | None = None

    # Cue stats
    cue_count: int = 0
    total_display_seconds: float = 0.0
    avg_duration_seconds: float = 0.0
    min_duration_seconds: float = 0.0
    max_duration_seconds: float = 0.0
    duration_buckets: DurationBuckets = field(default_factory=DurationBuckets)
    very_short_pct: float = 0.0          # share of cues < 0.5 s

    # Content stats
    total_characters: int = 0
    avg_chars_per_cue: float = 0.0
    estimated_wpm: float = 0.0           # words / minute of display time

    # Coverage — temporal distribution. last_cue_end_seconds is the
    # cheapest stand-in for "film duration" without probing the media.
    last_cue_end_seconds: float = 0.0
    speech_display_ratio_pct: float = 0.0
    coverage_buckets: list[CoverageBucket] = field(default_factory=list)


def compute_from_vtt(
    vtt_text: str,
    *,
    media_path: str | None = None,
    cache_key: str | None = None,
    mode: str | None = None,
    detected_source_language: str | None = None,
    took_seconds: float | None = None,
    pipeline_metrics: dict | None = None,
) -> VttStats:
    """Compute the full stats record from a .vtt's text.

    Caller passes through whatever it knows from the surrounding context
    (job runtime, cache key, payload metadata). What can't be passed in
    is parsed from the .vtt NOTE header line — the same header line the
    Cache Explorer parses for legacy entries that lack a media_path.
    """
    import time

    stats = VttStats(
        produced_at_epoch=time.time(),
        media_path=media_path,
        cache_key=cache_key,
        mode=mode,
        detected_source_language=detected_source_language,
        took_seconds=took_seconds,
        pipeline_metrics=pipeline_metrics,
    )
    if media_path:
        from pathlib import Path
        stats.media_name = Path(media_path).name

    # NOTE line: same shape as in the Cache Explorer.
    note_re = re.compile(
        r"NOTE Subtitle This auto-subs "
        r"\((?P<src>[a-z]{2}) -> (?P<tgt>[a-z]{2}), "
        r"mode=(?P<mode>[a-z]+), "
        r"whisper=(?P<whisper>[^,]+), "
        r"provider=(?P<provider>[^)]+)\)"
    )
    m = note_re.search(vtt_text)
    if m:
        stats.source_lang = m.group("src")
        stats.target_lang = m.group("tgt")
        if stats.mode is None:
            stats.mode = m.group("mode")
        stats.whisper_model = m.group("whisper")
        stats.provider = m.group("provider")

    cues = _parse_cues(vtt_text)
    stats.cue_count = len(cues)
    if not cues:
        return stats

    # ── Cue duration stats ──────────────────────────────────────────────
    durations = [max(0.0, e - s) for s, e, _ in cues]
    stats.total_display_seconds = round(sum(durations), 3)
    stats.avg_duration_seconds = round(sum(durations) / len(durations), 3)
    stats.min_duration_seconds = round(min(durations), 3)
    stats.max_duration_seconds = round(max(durations), 3)

    buckets = DurationBuckets()
    for d in durations:
        if d < 0.5:
            buckets.lt_0_5 += 1
        elif d < 1.0:
            buckets.lt_1_0 += 1
        elif d < 2.0:
            buckets.lt_2_0 += 1
        elif d < 5.0:
            buckets.lt_5_0 += 1
        else:
            buckets.gte_5_0 += 1
    stats.duration_buckets = buckets
    stats.very_short_pct = round(100.0 * buckets.lt_0_5 / len(durations), 1)

    # ── Content stats ───────────────────────────────────────────────────
    texts = [t for _, _, t in cues if t]
    total_chars = sum(len(t) for t in texts)
    stats.total_characters = total_chars
    stats.avg_chars_per_cue = round(total_chars / len(cues), 1) if cues else 0.0
    word_count = sum(len(t.split()) for t in texts)
    minutes = stats.total_display_seconds / 60.0
    stats.estimated_wpm = round(word_count / minutes, 1) if minutes > 0 else 0.0

    # ── Coverage stats ──────────────────────────────────────────────────
    stats.last_cue_end_seconds = round(max(e for _, e, _ in cues), 3)
    # "Speech display ratio" = how much of the apparent runtime is
    # covered by on-screen subtitle text. Not film runtime (we don't
    # probe the media) — runtime is approximated by the latest cue end.
    if stats.last_cue_end_seconds > 0:
        stats.speech_display_ratio_pct = round(
            100.0 * stats.total_display_seconds / stats.last_cue_end_seconds, 1,
        )

    # 10-minute coverage buckets covering [0, last_cue_end].
    bucket_count = max(1, int(stats.last_cue_end_seconds // 600) + 1)
    bucket_counts = [0] * bucket_count
    for s, _, _ in cues:
        idx = min(bucket_count - 1, int(s // 600))
        bucket_counts[idx] += 1
    stats.coverage_buckets = [
        CoverageBucket(start_min=i * 10, end_min=(i + 1) * 10, cue_count=c)
        for i, c in enumerate(bucket_counts)
    ]

    # ── Quality score ──────────────────────────────────────────────────
    # Computed last so it can see every other field populated. Kept on
    # the record (rather than recomputed by consumers) so the score is
    # stable across re-renders — what was written to the sidecar at
    # job completion stays the same string forever, even if the
    # scoring rules change in a future release.
    from app import quality as quality_mod
    stats.quality = quality_mod.to_jsonable(
        quality_mod.compute_quality_score(stats),
    )

    return stats


def to_jsonable(stats: VttStats) -> dict[str, Any]:
    """Flatten nested dataclasses to a JSON-safe dict for both the
    sidecar file and the API response."""
    return asdict(stats)


def write_sidecar(vtt_path, stats: VttStats) -> None:
    """Deprecated alias for backward compatibility — writes ``{vtt_path}.stats.json``
    next to the .vtt. Kept for tests that still exercise the legacy
    path. Production code uses ``write_cache_sidecar`` which lives
    inside cache_dir/stats/ so the user's movie folder stays clean."""
    import json
    import logging
    import os
    from pathlib import Path

    log = logging.getLogger("subtitle_this")
    target = Path(str(vtt_path) + ".stats.json")
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(to_jsonable(stats), indent=2))
        os.replace(tmp, target)
    except OSError as e:
        log.warning("stats sidecar write failed for %s: %s", target, e)


def cache_sidecar_path(cache_key: str) -> "Path":
    """Resolve the stats sidecar's on-disk path for a given VTT cache
    key. Single source of truth — both writer and Cache Explorer use
    this so a future relocation only changes one line."""
    from pathlib import Path
    from app.config import settings
    return Path(settings.cache_dir) / "stats" / f"{cache_key}.json"


def write_cache_sidecar(cache_key: str, stats: VttStats) -> None:
    """Atomically persist the stats record under
    ``cache_dir/stats/{cache_key}.json``. Same cache_key the VTT cache
    payload uses, so pair-lookup is trivial (both writers ran).

    Best-effort: IO errors are logged and swallowed. The Cache
    Explorer's stats page can still regenerate the record on demand
    from the VTT cache payload even if the sidecar is missing —
    the sidecar is a convenience for shell-level inspection and
    backup, not a correctness dependency."""
    import json
    import logging
    import os
    from pathlib import Path

    log = logging.getLogger("subtitle_this")
    target = cache_sidecar_path(cache_key)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(to_jsonable(stats), indent=2))
        os.replace(tmp, target)
    except OSError as e:
        log.warning("stats sidecar write failed for %s: %s", target, e)


def delete_cache_sidecar(cache_key: str) -> bool:
    """Remove the stats sidecar for a VTT cache key, if it exists.
    Returns True if a file was removed. Called from the Cache
    Explorer's delete path so a row's stats vanish with its parent."""
    target = cache_sidecar_path(cache_key)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        import logging
        logging.getLogger("subtitle_this").warning(
            "stats sidecar delete failed for %s", target, exc_info=True,
        )
        return False
