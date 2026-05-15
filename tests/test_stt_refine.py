"""Tests for the 0.8.0 confidence-gated re-transcription pass."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.pipeline import stt_refine
from app.pipeline.stt import Cue, TranscriptionResult


# ── Fixture helpers ─────────────────────────────────────────────────────────


def _cue(id_: int, start: float, end: float, text: str = "x",
         logprob: float | None = -0.3) -> Cue:
    return Cue(id=id_, start=start, end=end, text=text, avg_logprob=logprob)


def _result(cues: list[Cue]) -> TranscriptionResult:
    return TranscriptionResult(detected_language="en", cues=cues)


# ── Bucket math ─────────────────────────────────────────────────────────────


def test_bucket_coverage_zero_when_no_cues():
    b = stt_refine._Bucket(start=0.0, end=600.0)
    assert b.coverage == 0.0
    assert b.is_weak() is True


def test_bucket_coverage_full_when_continuous_dialog():
    b = stt_refine._Bucket(
        start=0.0, end=600.0,
        cue_count=100,
        display_seconds=590.0,
        cue_logprobs=[-0.2] * 100,
    )
    b.mean_logprob = -0.2
    assert b.coverage > 0.95
    assert b.is_weak() is False


def test_bucket_weak_on_low_coverage():
    b = stt_refine._Bucket(
        start=0.0, end=600.0,
        cue_count=10,
        display_seconds=60.0,    # 10% coverage — below 30% threshold
        cue_logprobs=[-0.3] * 10,
    )
    b.mean_logprob = -0.3
    assert b.is_weak() is True


def test_bucket_weak_on_low_logprob_even_with_good_coverage():
    """High coverage but mean logprob below -1.0 → still weak.
    Catches the "Whisper transcribed something but isn't sure what"
    case typical of accented or whispered dialogue."""
    b = stt_refine._Bucket(
        start=0.0, end=600.0,
        cue_count=50,
        display_seconds=500.0,   # 83% coverage — fine
        cue_logprobs=[-1.5] * 50,
    )
    b.mean_logprob = -1.5
    assert b.is_weak() is True


# ── _build_buckets ──────────────────────────────────────────────────────────


def test_build_buckets_partitions_cues_by_midpoint():
    cues = [
        _cue(0, 100.0, 102.0, logprob=-0.3),      # → bucket 0 (0-600)
        _cue(1, 700.0, 702.0, logprob=-0.4),      # → bucket 1 (600-1200)
        _cue(2, 1500.0, 1502.0, logprob=-0.5),    # → bucket 2 (1200-1800)
    ]
    buckets = stt_refine._build_buckets(cues, duration=1800.0)
    assert len(buckets) == 3
    assert buckets[0].cue_count == 1
    assert buckets[1].cue_count == 1
    assert buckets[2].cue_count == 1


def test_build_buckets_handles_zero_duration():
    assert stt_refine._build_buckets([], 0.0) == []


def test_build_buckets_computes_mean_logprob():
    cues = [
        _cue(0, 10.0, 12.0, logprob=-0.5),
        _cue(1, 20.0, 22.0, logprob=-1.5),
    ]
    buckets = stt_refine._build_buckets(cues, duration=600.0)
    assert buckets[0].cue_count == 2
    assert buckets[0].mean_logprob == pytest.approx(-1.0)


# ── _select_buckets_within_budget ───────────────────────────────────────────


def test_select_buckets_caps_at_20pct_budget():
    """20% of 3000s = 600s = 1 bucket. Selects only the worst."""
    buckets = [
        stt_refine._Bucket(start=0, end=600, cue_count=10,
                           display_seconds=100, mean_logprob=-0.5),
        stt_refine._Bucket(start=600, end=1200, cue_count=2,
                           display_seconds=20, mean_logprob=-2.0),    # worst
        stt_refine._Bucket(start=1200, end=1800, cue_count=5,
                           display_seconds=50, mean_logprob=-1.5),
    ]
    chosen = stt_refine._select_buckets_within_budget(buckets, duration=3000.0)
    # 600s budget → 1 bucket only. Worst (lowest coverage) wins.
    assert len(chosen) == 1
    assert chosen[0].start == 600.0


def test_select_buckets_orders_worst_first():
    buckets = [
        stt_refine._Bucket(start=0, end=300, cue_count=50,
                           display_seconds=200, mean_logprob=-0.4),    # coverage 0.67
        stt_refine._Bucket(start=300, end=600, cue_count=2,
                           display_seconds=20, mean_logprob=-1.5),     # coverage 0.07
    ]
    # 20% of 1500 = 300s budget → exactly one bucket fits.
    chosen = stt_refine._select_buckets_within_budget(buckets, duration=1500.0)
    assert len(chosen) == 1
    assert chosen[0].start == 300.0   # worst (0.07 coverage) wins


# ── refine_weak_buckets — early-outs (no STT needed) ────────────────────────


def test_refine_skips_when_no_cues(monkeypatch):
    """Defensive early-out — refine on empty result is a no-op."""
    res = _result([])
    out, stats = stt_refine.refine_weak_buckets(res, "/m/f.mkv", 1, 0.0)
    assert out is res
    assert stats.skipped_reason == "no_cues"


def test_refine_skips_when_no_logprob_data(monkeypatch):
    """OpenVINO backend doesn't expose avg_logprob. The refine phase
    can't make confidence-based decisions without it, so it bails out
    cleanly. The OpenVINO Silero-VAD path is the quality net there."""
    cues = [Cue(id=0, start=0, end=1, text="hi", avg_logprob=None)]
    res = _result(cues)
    out, stats = stt_refine.refine_weak_buckets(res, "/m/f.mkv", 1, 60.0)
    assert out is res
    assert stats.skipped_reason == "no_logprob_data"


def test_refine_skips_when_first_pass_clean(monkeypatch):
    """If overall coverage is >= 95% AND no bucket is weak, the refine
    phase is pure overhead — bail out without touching ffmpeg or STT."""
    # 600s bucket fully covered by a stack of 1s cues with healthy logprob.
    cues = [_cue(i, float(i), float(i) + 0.99, logprob=-0.3) for i in range(600)]
    res = _result(cues)
    out, stats = stt_refine.refine_weak_buckets(res, "/m/f.mkv", 1, 600.0)
    assert out is res
    assert stats.skipped_reason == "first_pass_clean"


def test_refine_skips_when_no_buckets_in_budget(monkeypatch):
    """Edge case: weak buckets exist but they're all single cues so the
    budget=0 case kicks in. Verify safe skip."""
    # The "budget" guard is buckets that fit; this test confirms we
    # don't loop on an empty selection.
    cues = [_cue(0, 10.0, 10.5, logprob=-2.5)]   # 1 cue, very low logprob
    res = _result(cues)
    # Tiny audio (50s) — bucket is 0-50, 20% budget = 10s. The bucket
    # spans 0-50s (50s > 10s budget) → can't fit → skip.
    out, stats = stt_refine.refine_weak_buckets(res, "/m/f.mkv", 1, 50.0)
    assert stats.skipped_reason == "no_buckets_in_budget"


# ── refine_weak_buckets — full re-pass path ─────────────────────────────────


def test_refine_re_decodes_weak_bucket_and_merges(monkeypatch, tmp_path):
    """End-to-end: a weak bucket triggers ffmpeg extract + aggressive
    transcribe + merge. Verify the new cues land in place of the
    weak-bucket originals.

    Audio: 3000s (5 × 10-min buckets). 20% budget = 600s = exactly 1
    bucket fits. Buckets 0, 2, 3, 4 are densely populated and healthy;
    bucket 1 (600-1200s) has 2 cues with terrible logprob → weak,
    selected for refine."""
    from app.config import settings as runtime_settings
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": cache_dir},
    )

    # Helper: dense, healthy cues filling a 600s bucket.
    def healthy_bucket(start: float, id_offset: int):
        return [
            _cue(id_offset + i, start + float(i * 2 + 1),
                 start + float(i * 2 + 2), logprob=-0.3)
            for i in range(200)
        ]
    first_pass = (
        healthy_bucket(0.0, 0) +
        # Bucket 1 (600-1200): only 2 cues with terrible logprob → weak.
        [_cue(200, 800.0, 801.0, logprob=-2.5, text="weak1"),
         _cue(201, 900.0, 901.0, logprob=-2.5, text="weak2")] +
        healthy_bucket(1200.0, 300) +
        healthy_bucket(1800.0, 500) +
        healthy_bucket(2400.0, 700)
    )
    res = _result(first_pass)

    # Stub ffmpeg extract (creates a fake WAV file).
    def fake_extract(media_path, track_index, start, end):
        fake_wav = tmp_path / f"refine-{start}-{end}.wav"
        fake_wav.write_bytes(b"\x00" * 16)
        return fake_wav
    monkeypatch.setattr(stt_refine, "_extract_audio_range", fake_extract)

    # Stub the aggressive transcribe — returns better cues for the bucket.
    aggressive_calls: list = []
    def fake_transcribe(path, language_hint=None, check_cancel=None,
                        aggressive=False, **_):
        aggressive_calls.append({"path": path, "aggressive": aggressive})
        # Return MORE cues than the first pass had (so the safety
        # "fewer cues → keep first pass" check doesn't trigger).
        return TranscriptionResult(
            detected_language="en",
            cues=[
                # Timestamps are RELATIVE to the bucket start — refine
                # will offset them back to absolute.
                Cue(id=0, start=10.0, end=11.0, text="recovered_a", avg_logprob=-0.5),
                Cue(id=1, start=50.0, end=51.0, text="recovered_b", avg_logprob=-0.4),
                Cue(id=2, start=100.0, end=101.0, text="recovered_c", avg_logprob=-0.3),
            ],
        )
    monkeypatch.setattr(stt_refine.stt_dispatcher, "transcribe", fake_transcribe)

    out, stats = stt_refine.refine_weak_buckets(
        res, "/m/f.mkv", 1, audio_duration_seconds=3000.0,
    )

    assert stats.buckets_weak >= 1
    assert stats.buckets_refined == 1     # exactly 1 bucket fit the budget
    assert stats.cues_added > 0
    # The aggressive flag MUST have been set on the re-pass.
    assert all(c["aggressive"] is True for c in aggressive_calls)
    # The new cues should have absolute timestamps in the weak bucket's
    # range (around 600-1200s).
    new_texts = [c.text for c in out.cues]
    assert "recovered_a" in new_texts
    # Old weak cues should be GONE (replaced).
    assert "weak1" not in new_texts
    assert "weak2" not in new_texts


def test_refine_keeps_first_pass_when_aggressive_returns_fewer(monkeypatch, tmp_path):
    """Safety contract: if the aggressive re-pass produces FEWER cues
    than the first pass had for a bucket, we keep the first-pass
    result. The re-pass is meant to recover dropped dialog, not lose
    existing dialog.

    Setup: 3000s audio (5 buckets), all densely covered EXCEPT
    bucket 1 (600-1200s) which has 5 cues with bad logprob. Budget
    20 % = 600 s = exactly one bucket → bucket 1 selected."""
    from app.config import settings as runtime_settings
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": cache_dir},
    )

    def healthy_bucket(start: float, id_offset: int):
        return [
            _cue(id_offset + i, start + float(i * 2 + 1),
                 start + float(i * 2 + 2), logprob=-0.3)
            for i in range(200)
        ]
    weak_in_bucket1 = [
        _cue(200 + i, float(800 + i * 30), float(801 + i * 30),
             logprob=-2.0, text=f"original_{i}")
        for i in range(5)
    ]
    first_pass = (
        healthy_bucket(0.0, 0) +
        weak_in_bucket1 +
        healthy_bucket(1200.0, 300) +
        healthy_bucket(1800.0, 500) +
        healthy_bucket(2400.0, 700)
    )
    res = _result(first_pass)

    def fake_extract(media_path, track_index, start, end):
        fake_wav = tmp_path / f"refine-{start}-{end}.wav"
        fake_wav.write_bytes(b"\x00" * 16)
        return fake_wav
    monkeypatch.setattr(stt_refine, "_extract_audio_range", fake_extract)

    # Aggressive pass returns ONE cue — fewer than the first pass had.
    def fake_transcribe(path, **_):
        return TranscriptionResult(
            detected_language="en",
            cues=[Cue(id=0, start=10.0, end=11.0, text="just one", avg_logprob=-0.5)],
        )
    monkeypatch.setattr(stt_refine.stt_dispatcher, "transcribe", fake_transcribe)

    out, stats = stt_refine.refine_weak_buckets(
        res, "/m/f.mkv", 1, audio_duration_seconds=3000.0,
    )

    # All 5 original cues survive — the safety guard rejected the re-pass.
    out_texts = [c.text for c in out.cues]
    assert all(f"original_{i}" in out_texts for i in range(5))
    assert "just one" not in out_texts
    assert stats.buckets_refined == 0


def test_refine_renumbers_cue_ids_after_merge(monkeypatch, tmp_path):
    """Post-merge, cue ids must be contiguous 0..N-1. Downstream code
    (cache key, polish, translate) depends on this invariant. Same
    3000s/1-budget-bucket setup as the merge test."""
    from app.config import settings as runtime_settings
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": cache_dir},
    )

    def healthy_bucket(start: float, id_offset: int):
        return [
            _cue(id_offset + i, start + float(i * 2 + 1),
                 start + float(i * 2 + 2), logprob=-0.3)
            for i in range(200)
        ]
    first_pass = (
        healthy_bucket(0.0, 0) +
        [_cue(200, 800.0, 801.0, logprob=-2.5, text="weak1"),
         _cue(201, 900.0, 901.0, logprob=-2.5, text="weak2")] +
        healthy_bucket(1200.0, 300) +
        healthy_bucket(1800.0, 500) +
        healthy_bucket(2400.0, 700)
    )
    res = _result(first_pass)

    def fake_extract(media_path, track_index, start, end):
        fake_wav = tmp_path / f"refine-{start}-{end}.wav"
        fake_wav.write_bytes(b"\x00" * 16)
        return fake_wav
    monkeypatch.setattr(stt_refine, "_extract_audio_range", fake_extract)

    def fake_transcribe(path, **_):
        return TranscriptionResult(
            detected_language="en",
            cues=[
                Cue(id=99, start=10.0, end=11.0, text="a", avg_logprob=-0.5),
                Cue(id=42, start=50.0, end=51.0, text="b", avg_logprob=-0.5),
                Cue(id=7,  start=100.0, end=101.0, text="c", avg_logprob=-0.5),
            ],
        )
    monkeypatch.setattr(stt_refine.stt_dispatcher, "transcribe", fake_transcribe)

    out, _ = stt_refine.refine_weak_buckets(
        res, "/m/f.mkv", 1, audio_duration_seconds=3000.0,
    )

    ids = [c.id for c in out.cues]
    assert ids == list(range(len(out.cues)))


def test_refine_handles_ffmpeg_failure_gracefully(monkeypatch, tmp_path):
    """Safety: if ffmpeg fails extracting a bucket (corrupt source,
    permission error), skip that bucket and move on. Don't crash the
    whole job."""
    import subprocess as sub_mod
    from app.config import settings as runtime_settings
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": cache_dir},
    )

    weak_cues = [
        _cue(0, 800.0, 801.0, logprob=-2.5, text="weak1"),
        _cue(1, 900.0, 901.0, logprob=-2.5, text="weak2"),
    ]
    res = _result(weak_cues)

    def boom(*a, **kw):
        raise sub_mod.CalledProcessError(1, ["ffmpeg"], stderr=b"corrupt")
    monkeypatch.setattr(stt_refine, "_extract_audio_range", boom)

    # Should not raise — failure is logged and skipped.
    out, stats = stt_refine.refine_weak_buckets(
        res, "/m/f.mkv", 1, audio_duration_seconds=6000.0,
    )
    # Original cues survive.
    assert [c.text for c in out.cues] == ["weak1", "weak2"]
    assert stats.buckets_refined == 0
