"""Tests for app/pipeline_metrics.py.

The metrics are the single source of evidence for the three pathologies
the Inception post-mortem identified — VAD too strict, packing pad-drop,
Whisper compressed timestamps. Each aggregator covers one cause; the
finalize() outputs are what the stats sidecar and the UI render. We
lock in their math here so a future refactor can't silently break the
diagnostic signal.
"""
from __future__ import annotations

from app.pipeline_metrics import (
    PackingAggregator, VadAggregator, WhisperAggregator,
    _BIN_LABELS, to_jsonable, PipelineMetrics,
)


# ── VadAggregator ──────────────────────────────────────────────────────────


def test_vad_empty_run_yields_zero_metrics():
    """A pipeline that processed zero audio (smoke test for plumbing)
    must still produce a valid record with explicit zeros, not a half-
    populated mix of None and floats."""
    m = VadAggregator().finalize()
    assert m.total_audio_seconds == 0.0
    assert m.region_count == 0
    assert m.speech_ratio_pct == 0.0
    assert m.region_duration_histogram == {}


def test_vad_speech_ratio_is_speech_over_audio():
    """The headline ratio operators use to spot VAD under-detection.
    35 % of 100 s of audio = 35 s of speech."""
    agg = VadAggregator()
    agg.observe(seg_audio_seconds=100.0, regions=[(0, 16000 * 35)],
                sample_rate=16000)
    m = agg.finalize()

    assert m.total_audio_seconds == 100.0
    assert m.total_speech_seconds_detected == 35.0
    assert m.speech_ratio_pct == 35.0


def test_vad_aggregates_across_multiple_segments():
    """The STT loop calls observe() once per 600 s segment iteration —
    the totals must sum correctly across calls."""
    agg = VadAggregator()
    agg.observe(seg_audio_seconds=600.0, regions=[(0, 16000 * 60)],
                sample_rate=16000)
    agg.observe(seg_audio_seconds=600.0, regions=[(0, 16000 * 40)],
                sample_rate=16000)
    m = agg.finalize()

    assert m.total_audio_seconds == 1200.0
    assert m.total_speech_seconds_detected == 100.0
    assert m.region_count == 2
    assert m.speech_ratio_pct == round(100 * 100 / 1200, 1)


def test_vad_histogram_bins_at_boundary_edges():
    """The diagnostic bucket is 0.25-0.5 s — regions barely above
    Silero's min_speech_duration_ms=250 floor. One region per band
    so each bucket increments exactly once, no off-by-one at the
    band edges."""
    agg = VadAggregator()
    # Region durations: 0.2, 0.4, 0.7, 2.0, 5.0, 12.0 — one per band
    # plus one to land in lt_0_25s (which Silero shouldn't normally
    # produce but we verify the bin classification anyway).
    for dur_s in [0.2, 0.4, 0.7, 2.0, 5.0, 12.0]:
        samples = int(dur_s * 16000)
        agg.observe(seg_audio_seconds=20.0, regions=[(0, samples)],
                    sample_rate=16000)

    m = agg.finalize()
    hist = m.region_duration_histogram
    assert hist == {
        "lt_0_25s": 1,
        "0_25_to_0_5s": 1,
        "0_5_to_1s": 1,
        "1_to_3s": 1,
        "3_to_10s": 1,
        "gte_10s": 1,
    }


def test_vad_short_region_pct_signals_pathology():
    """When most regions are in the barely-passed zone (< 0.5 s),
    short_region_pct is the metric that surfaces the "VAD trimming
    syllables" pathology. 4 short / 1 normal = 80 %."""
    agg = VadAggregator()
    # 4 regions in the [0.25, 0.5) band, 1 in [1, 3)
    for _ in range(4):
        agg.observe(seg_audio_seconds=2.0,
                    regions=[(0, int(0.3 * 16000))], sample_rate=16000)
    agg.observe(seg_audio_seconds=2.0,
                regions=[(0, int(2.0 * 16000))], sample_rate=16000)

    m = agg.finalize()
    assert m.short_region_pct == 80.0


def test_vad_median_is_robust_to_outliers():
    """An odd-count list returns the middle value; an even-count
    averages the two middles. Both branches need to be hit because
    the user-facing median surfaces "typical region length" — a
    single 30 s outlier shouldn't shift it."""
    agg = VadAggregator()
    for dur in [0.4, 1.0, 1.5, 2.0, 30.0]:   # 5 values → middle is 1.5
        agg.observe(seg_audio_seconds=40.0,
                    regions=[(0, int(dur * 16000))], sample_rate=16000)
    assert agg.finalize().median_region_seconds == 1.5

    agg2 = VadAggregator()
    for dur in [0.4, 1.0, 1.5, 2.0]:   # 4 values → avg of 1.0 and 1.5
        agg2.observe(seg_audio_seconds=40.0,
                     regions=[(0, int(dur * 16000))], sample_rate=16000)
    assert agg2.finalize().median_region_seconds == 1.25


# ── PackingAggregator ──────────────────────────────────────────────────────


def test_packing_counts_window_classes():
    """Single-region vs packed windows are tracked separately because
    only packed windows can suffer pad-drop. The cue keeper / drop
    counts let the UI compute the drop-share metric directly."""
    agg = PackingAggregator(enabled=True)
    agg.record_window(n_regions=1)         # single-region
    agg.record_window(n_regions=1)         # single-region
    agg.record_window(n_regions=3)         # packed
    agg.record_window(n_regions=5)         # packed
    for _ in range(7):
        agg.record_cue_keep()
    for _ in range(3):
        agg.record_cue_drop_pad_zone()

    m = agg.finalize()
    assert m.windows_total == 4
    assert m.windows_single_region == 2
    assert m.windows_packed == 2
    assert m.avg_regions_per_window == round((1 + 1 + 3 + 5) / 4, 2)
    assert m.cue_keep_count == 7
    assert m.cue_drop_pad_zone_count == 3
    assert m.enabled is True


def test_packing_disabled_run_records_enabled_false():
    """When the user turns region_packing OFF in Settings the
    aggregator carries that fact through to the sidecar so a viewer
    can tell which configuration produced the numbers."""
    agg = PackingAggregator(enabled=False)
    agg.record_window(n_regions=1)
    m = agg.finalize()
    assert m.enabled is False


def test_packing_empty_finalize_doesnt_divide_by_zero():
    """A run with zero decoded windows (degenerate but plausible — all
    audio was silence) must yield avg_regions_per_window=0.0, not
    crash on the division."""
    m = PackingAggregator(enabled=True).finalize()
    assert m.windows_total == 0
    assert m.avg_regions_per_window == 0.0


# ── WhisperAggregator ─────────────────────────────────────────────────────


def test_whisper_aggregator_counts_degenerate_drops():
    """The on_drop callback wired into _parse_segments increments this
    counter once per degenerate-timestamp cue. A spike here corroborates
    the very-short-cue signal at the .vtt level."""
    agg = WhisperAggregator()
    for _ in range(7):
        agg.record_degenerate_timestamp_drop()
    assert agg.finalize().cue_drop_degenerate_timestamp_count == 7


# ── Serialization ──────────────────────────────────────────────────────────


def test_to_jsonable_produces_nested_dict():
    """The sidecar and the API both serialize via to_jsonable. None
    sub-records must round-trip as null, not be silently coerced into
    an empty struct — downstream needs to tell "phase ran but found
    zero" apart from "phase wasn't instrumented in this run"."""
    pm = PipelineMetrics(
        vad=VadAggregator().finalize(),
        packing=None,
        whisper=WhisperAggregator().finalize(),
    )
    d = to_jsonable(pm)
    assert d["packing"] is None
    assert isinstance(d["vad"], dict)
    assert d["vad"]["region_count"] == 0
    assert isinstance(d["whisper"], dict)
