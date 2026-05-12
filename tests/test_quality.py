"""Tests for app/quality.py — heuristic quality score.

The scoring rules are the operator-facing summary of "what would have
made the Inception 0.7.1 output trustworthy". Each penalty corresponds
to a specific pathology we've actually seen in the wild; the tests
lock in the trigger thresholds so a refactor doesn't silently soften
the diagnostic.
"""
from __future__ import annotations

from app.quality import (
    QualityFactor, QualityScore,
    compute_quality_score, to_jsonable, _grade_for,
)


class _Stats:
    """Lightweight VttStats stand-in — duck-typed in compute_quality_score
    so we don't have to construct the full dataclass for each test."""
    def __init__(self, **kwargs):
        self.cue_count = kwargs.get("cue_count", 1000)
        self.very_short_pct = kwargs.get("very_short_pct", 0.0)
        self.pipeline_metrics = kwargs.get("pipeline_metrics")


def test_clean_run_scores_full_100():
    """A run with zero detected pathologies hits the ceiling — five
    stars, A grade, summary describes it as clean."""
    s = compute_quality_score(_Stats())
    assert s.score == 100
    assert s.stars == 5
    assert s.grade == "A"
    assert s.factors == []
    assert "Clean" in s.summary


# ── (1) Compressed timestamps ──────────────────────────────────────────────


def test_very_short_pct_above_25_triggers_critical_penalty():
    """28.6 % is the Inception number — meant to land as critical."""
    s = compute_quality_score(_Stats(very_short_pct=28.6))
    assert any(f.name == "Compressed timestamps" and f.severity == "critical"
               for f in s.factors)
    assert s.score == 85   # 100 - 15


def test_very_short_pct_15_to_25_triggers_warn():
    s = compute_quality_score(_Stats(very_short_pct=20.0))
    f = next(f for f in s.factors if f.name == "Compressed timestamps")
    assert f.severity == "warn"
    assert s.score == 92   # 100 - 8


def test_very_short_pct_below_15_skips_penalty():
    """The threshold is exclusive — exactly-15 still passes."""
    s = compute_quality_score(_Stats(very_short_pct=10.0))
    assert not any(f.name == "Compressed timestamps" for f in s.factors)
    assert s.score == 100


# ── (2) Packing pad-drops ──────────────────────────────────────────────────


def test_pad_drop_above_20_pct_is_critical():
    pm = {"packing": {"cue_drop_pad_zone_count": 300, "cue_keep_count": 700}}
    s = compute_quality_score(_Stats(pipeline_metrics=pm))
    f = next(f for f in s.factors if f.name == "Region-packing pad-drops")
    assert f.severity == "critical"
    assert s.score == 80   # 100 - 20


def test_pad_drop_zero_skips_penalty():
    pm = {"packing": {"cue_drop_pad_zone_count": 0, "cue_keep_count": 900}}
    s = compute_quality_score(_Stats(pipeline_metrics=pm))
    assert not any(f.name == "Region-packing pad-drops" for f in s.factors)


# ── (3) VAD under-detection ───────────────────────────────────────────────


def test_vad_speech_ratio_below_20_pct_is_critical():
    """The "VAD too strict for film mix" diagnostic. Audio must be
    long enough that the ratio isn't noise (>60 s)."""
    pm = {"vad": {"speech_ratio_pct": 15.0, "total_audio_seconds": 8000.0,
                  "short_region_pct": 5.0}}
    s = compute_quality_score(_Stats(pipeline_metrics=pm))
    f = next(f for f in s.factors if f.name == "VAD under-detection")
    assert f.severity == "critical"
    assert s.score == 85


def test_vad_short_audio_skips_speech_ratio_check():
    """Test clips and 30 s previews shouldn't be penalized for low
    speech ratios — they're noise, not a diagnostic."""
    pm = {"vad": {"speech_ratio_pct": 10.0, "total_audio_seconds": 30.0,
                  "short_region_pct": 5.0}}
    s = compute_quality_score(_Stats(pipeline_metrics=pm))
    assert not any("VAD" in f.name and "detection" in f.name for f in s.factors)


def test_vad_short_region_above_40_pct_flags_word_trimming():
    pm = {"vad": {"speech_ratio_pct": 45.0, "total_audio_seconds": 8000.0,
                  "short_region_pct": 50.0}}
    s = compute_quality_score(_Stats(pipeline_metrics=pm))
    f = next(f for f in s.factors if f.name == "VAD trimming short words")
    assert f.severity == "warn"


# ── (4) Whisper degenerate timestamps ─────────────────────────────────────


def test_whisper_hallucination_rate_above_20_per_100_warns():
    pm = {"whisper": {"cue_drop_degenerate_timestamp_count": 250}}
    s = compute_quality_score(_Stats(cue_count=1000, pipeline_metrics=pm))
    f = next(f for f in s.factors if f.name == "Whisper hallucinations")
    assert f.severity == "warn"
    assert s.score == 90


# ── (5) Translation empty / duplicate / mismatch ──────────────────────────


def test_empty_translations_above_10_pct_is_critical():
    """The NLLB int8 quantization degenerate signature. Heaviest single
    penalty in the scoring system because the output is unusable."""
    pm = {"translation": {"output_cue_count": 1000, "empty_output_count": 150,
                          "duplicate_output_count": 0, "input_cue_count": 1000}}
    s = compute_quality_score(_Stats(pipeline_metrics=pm))
    f = next(f for f in s.factors if f.name == "Empty translations")
    assert f.severity == "critical"
    assert f.penalty == 25
    assert s.score == 75


def test_duplicate_translations_above_30_pct_warn():
    pm = {"translation": {"output_cue_count": 1000, "empty_output_count": 0,
                          "duplicate_output_count": 400, "input_cue_count": 1000}}
    s = compute_quality_score(_Stats(pipeline_metrics=pm))
    f = next(f for f in s.factors if f.name == "Duplicate translations")
    assert f.severity == "warn"


def test_cue_count_mismatch_is_critical():
    pm = {"translation": {"input_cue_count": 1000, "output_cue_count": 800,
                          "empty_output_count": 0, "duplicate_output_count": 0}}
    s = compute_quality_score(_Stats(pipeline_metrics=pm))
    assert any(f.name == "Cue count mismatch" and f.severity == "critical"
               for f in s.factors)


# ── Composite + edges ─────────────────────────────────────────────────────


def test_multiple_pathologies_compound():
    """Three issues at once — the score should reflect the cumulative
    cost, not just the largest single penalty. Inception-like profile:
    compressed timestamps (15) + heavy pad-drops (20) + low speech
    ratio (15) → 50 deducted."""
    pm = {
        "packing": {"cue_drop_pad_zone_count": 250, "cue_keep_count": 750},
        "vad": {"speech_ratio_pct": 15.0, "total_audio_seconds": 8000.0,
                "short_region_pct": 5.0},
    }
    s = compute_quality_score(_Stats(very_short_pct=30.0, pipeline_metrics=pm))
    assert s.score == 50   # 100 - 15 - 20 - 15
    assert s.grade == "D"
    assert s.stars == 2    # 50/20 = 2.5 → rounds to 2 (banker's rounding)


def test_score_clamps_at_zero():
    """A run could in theory deduct more than 100 points. Clamp."""
    pm = {
        "packing": {"cue_drop_pad_zone_count": 800, "cue_keep_count": 200},
        "translation": {"input_cue_count": 1000, "output_cue_count": 1000,
                        "empty_output_count": 500, "duplicate_output_count": 600},
        "vad": {"speech_ratio_pct": 5.0, "total_audio_seconds": 8000.0,
                "short_region_pct": 60.0},
    }
    s = compute_quality_score(_Stats(very_short_pct=30.0, pipeline_metrics=pm))
    assert s.score == 0
    assert s.stars == 0
    assert s.grade == "F"


def test_grade_band_boundaries():
    assert _grade_for(100) == "A"
    assert _grade_for(90) == "A"
    assert _grade_for(89) == "B"
    assert _grade_for(75) == "B"
    assert _grade_for(74) == "C"
    assert _grade_for(60) == "C"
    assert _grade_for(59) == "D"
    assert _grade_for(45) == "D"
    assert _grade_for(44) == "F"
    assert _grade_for(0) == "F"


def test_to_jsonable_returns_flat_dict():
    """The sidecar serializer must round-trip the factor list to nested
    dicts so consumers without our dataclass imports can read it."""
    score = QualityScore(
        score=70, stars=4, grade="C",
        summary="ok",
        factors=[QualityFactor(name="x", severity="warn", penalty=10, detail="y")],
    )
    d = to_jsonable(score)
    assert d["score"] == 70
    assert d["factors"][0]["name"] == "x"
    assert isinstance(d["factors"], list)
