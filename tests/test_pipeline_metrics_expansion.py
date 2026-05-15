"""Tests for the 0.8.1 metrics expansion (audio_prep / anti_hallucination /
polish + refine surfacing on the stats page).

The metrics work isn't load-bearing on subtitle correctness, but it IS
the only on-disk record of which optimisations fired for a given run.
A future operator reading the Cache Explorer stats page needs the
counts to actually reflect what the pipeline did — these tests pin
that contract so a refactor of the producer side can't silently zero
out a stats column.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.pipeline_metrics import (
    AudioPrepMetrics, AntiHallucinationMetrics, PolishMetrics,
    PipelineMetrics, to_jsonable,
)


# ── AudioPrepMetrics ────────────────────────────────────────────────────────


def test_audio_prep_extract_records_center_channel_for_51(tmp_path, monkeypatch):
    """5.1 source → extract_audio populates prep_stats with
    used_center_channel=True, loudnorm_applied=True, no fallback."""
    from app.config import settings as runtime_settings
    from app.pipeline import audio as audio_mod

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": cache_dir},
    )

    def fake_run(args, **kwargs):
        cp = MagicMock()
        if "ffprobe" in args[0]:
            cp.stdout = json.dumps({"streams": [{"channels": 6, "channel_layout": "5.1"}]})
        cp.returncode = 0
        return cp
    monkeypatch.setattr(audio_mod.subprocess, "run", fake_run)

    sink: dict = {}
    with audio_mod.extract_audio("/m/f.mkv", 1, prep_stats=sink):
        pass
    assert sink["source_channels"] == 6
    assert sink["source_channel_layout"] == "5.1"
    assert sink["used_center_channel"] is True
    assert sink["loudnorm_applied"] is True
    assert sink["optimised_chain_failed"] is False


def test_audio_prep_extract_records_downmix_for_stereo(tmp_path, monkeypatch):
    """Stereo source → used_center_channel=False (downmix path)."""
    from app.config import settings as runtime_settings
    from app.pipeline import audio as audio_mod

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": cache_dir},
    )

    def fake_run(args, **kwargs):
        cp = MagicMock()
        if "ffprobe" in args[0]:
            cp.stdout = json.dumps({"streams": [{"channels": 2, "channel_layout": "stereo"}]})
        cp.returncode = 0
        return cp
    monkeypatch.setattr(audio_mod.subprocess, "run", fake_run)

    sink: dict = {}
    with audio_mod.extract_audio("/m/f.mkv", 1, prep_stats=sink):
        pass
    assert sink["source_channels"] == 2
    assert sink["used_center_channel"] is False
    assert sink["loudnorm_applied"] is True
    assert sink["optimised_chain_failed"] is False


def test_audio_prep_dataclass_carries_vocal_isolation_auto_skipped_flag():
    """0.9.1 added a vocal_isolation_auto_skipped field so the stats page
    can explain why the Vocal isolation block didn't render even though
    the user enabled it in Settings. Pin the field name + default so a
    rename doesn't silently break the template branch."""
    m = AudioPrepMetrics(
        source_channels=6,
        source_channel_layout="5.1",
        used_center_channel=True,
        loudnorm_applied=True,
        optimised_chain_failed=False,
        vocal_isolation_auto_skipped=True,
    )
    assert m.vocal_isolation_auto_skipped is True
    # Default must be False so legacy entries deserialize cleanly.
    assert AudioPrepMetrics().vocal_isolation_auto_skipped is False
    # Must round-trip through asdict / _pm_from_dict.
    from app.transcript_cache import _pm_from_dict
    payload = {"audio_prep": {
        "source_channels": 6,
        "source_channel_layout": "5.1",
        "used_center_channel": True,
        "loudnorm_applied": True,
        "optimised_chain_failed": False,
        "vocal_isolation_auto_skipped": True,
    }}
    revived = _pm_from_dict(payload)
    assert revived.audio_prep.vocal_isolation_auto_skipped is True


def test_audio_prep_extract_records_fallback_on_optimised_chain_failure(
    tmp_path, monkeypatch,
):
    """ffprobe says 5.1 but ffmpeg rejects the pan filter → we fall
    back to plain downmix and the stats sink reflects the demotion."""
    import subprocess as sub_mod
    from app.config import settings as runtime_settings
    from app.pipeline import audio as audio_mod

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": cache_dir},
    )

    def fake_run(args, **kwargs):
        if "ffprobe" in args[0]:
            cp = MagicMock()
            cp.stdout = json.dumps({"streams": [{"channels": 6, "channel_layout": "5.1"}]})
            cp.returncode = 0
            return cp
        # First ffmpeg call (pan=FC) fails; fallback (plain downmix)
        # succeeds.
        af_arg = args[args.index("-af") + 1] if "-af" in args else ""
        if "pan=mono|c0=FC" in af_arg:
            raise sub_mod.CalledProcessError(1, args, stderr=b"no FC")
        cp = MagicMock()
        cp.returncode = 0
        return cp
    monkeypatch.setattr(audio_mod.subprocess, "run", fake_run)

    sink: dict = {}
    with audio_mod.extract_audio("/m/f.mkv", 1, prep_stats=sink):
        pass
    assert sink["source_channels"] == 6
    # The fallback path ran — sink reflects the demoted state.
    assert sink["used_center_channel"] is False
    assert sink["optimised_chain_failed"] is True


# ── AntiHallucinationMetrics ────────────────────────────────────────────────


def test_anti_hallucination_filter_records_split_drops():
    """The FilterStats record splits blacklist drops vs repetition
    drops so the stats page can tell a YouTube-tail-heavy run from
    a stuck-loop-heavy run."""
    from app.pipeline.anti_hallucination import filter_cues
    from app.pipeline.stt import Cue

    cues = [
        Cue(id=0, start=0.0, end=1.0, text="Hello there."),                  # keep
        Cue(id=1, start=1.0, end=2.0, text="Thanks for watching."),          # blacklist
        Cue(id=2, start=2.0, end=3.0, text="Yeah yeah yeah yeah yeah."),     # repetition
        Cue(id=3, start=3.0, end=4.0, text="Subscribe."),                    # blacklist
        Cue(id=4, start=4.0, end=5.0, text="How are you?"),                  # keep
    ]
    out, stats = filter_cues(cues)
    assert stats.input_count == 5
    assert stats.blacklisted == 2
    assert stats.repetition_dropped == 1
    assert stats.output_count == 2
    assert stats.safety_bailout is False
    assert len(out) == 2


def test_anti_hallucination_safety_bailout_preserves_original_cues():
    """When >=90% would be dropped on an input of >=10 cues, the
    filter returns the ORIGINAL list and stamps safety_bailout=True
    on the stats. The drop counts describe what WOULD have been
    dropped, not what actually was."""
    from app.pipeline.anti_hallucination import filter_cues
    from app.pipeline.stt import Cue

    # 10 cues, all blacklisted → 100% drop → triggers safety net.
    cues = [
        Cue(id=i, start=float(i), end=float(i) + 1.0, text="Thanks for watching.")
        for i in range(10)
    ]
    out, stats = filter_cues(cues)
    assert stats.safety_bailout is True
    assert stats.blacklisted == 10
    # Originals preserved — output_count matches input.
    assert stats.output_count == 10
    assert len(out) == 10


# ── PolishMetrics ───────────────────────────────────────────────────────────


def test_polish_with_stats_reports_merge_and_extend_counts(monkeypatch):
    """polish_cues_with_stats should report cues_merged and
    cues_extended so the stats page can quantify the polish work."""
    from app.config import settings
    from app.pipeline.polish import polish_cues_with_stats
    from app.pipeline.stt import Cue

    # Force-enable polish + merge for the test via the canonical
    # ``_overrides`` channel — directly setattr-ing the settings
    # object leaks across tests because monkeypatch can't restore
    # an attribute that didn't exist before.
    monkeypatch.setattr(
        settings, "_overrides",
        {
            **settings._overrides,
            "polish_enabled": True,
            "merge_adjacent_cues": True,
            "max_gap_to_merge_seconds": 0.5,
            "max_merged_cue_duration_seconds": 7.0,
            "max_line_chars": 80,
            "max_lines_per_cue": 2,
            "min_cue_duration_seconds": 2.0,
            "min_seconds_per_char": 0.0,
            "cue_separation_seconds": 0.05,
        },
    )

    # Two cues 0.1s apart with short text → will merge into one.
    # A third cue, isolated → won't merge but will get extended to 2 s.
    cues = [
        Cue(id=0, start=0.0, end=0.4, text="Yes."),
        Cue(id=1, start=0.5, end=0.9, text="No."),     # merges into #0
        Cue(id=2, start=10.0, end=10.5, text="Maybe."),  # extends (was 0.5 s)
    ]
    out, stats = polish_cues_with_stats(cues)
    assert stats.enabled is True
    assert stats.input_count == 3
    assert stats.output_count == 2          # one pair merged
    assert stats.cues_merged == 1
    assert stats.cues_extended >= 1         # the standalone cue got extended


def test_polish_with_stats_when_disabled_reports_zero_counts(monkeypatch):
    """When polish is disabled, the stats record carries enabled=False
    and zero counts so the stats page can distinguish 'ran with no
    edits' from 'never ran'."""
    from app.config import settings
    from app.pipeline.polish import polish_cues_with_stats
    from app.pipeline.stt import Cue

    monkeypatch.setattr(
        settings, "_overrides",
        {**settings._overrides, "polish_enabled": False},
    )

    cues = [Cue(id=0, start=0.0, end=0.3, text="Brief.")]
    out, stats = polish_cues_with_stats(cues)
    assert stats.enabled is False
    assert stats.cues_merged == 0
    assert stats.cues_extended == 0
    # Original cue passed through untouched.
    assert out[0].end == 0.3


# ── Rehydration round-trip via transcript_cache._pm_from_dict ────────────────


def test_pm_from_dict_rehydrates_audio_prep_anti_hallucination_polish():
    """A persisted PipelineMetrics with all the new fields must come
    back from JSON intact — operators inspecting an old cached run
    after a redeploy should see the same numbers."""
    from app.transcript_cache import _pm_from_dict

    original = PipelineMetrics(
        audio_prep=AudioPrepMetrics(
            source_channels=6, source_channel_layout="5.1",
            used_center_channel=True, loudnorm_applied=True,
            optimised_chain_failed=False,
        ),
        anti_hallucination=AntiHallucinationMetrics(
            input_count=100, blacklist_dropped=3, repetition_dropped=2,
            output_count=95, safety_bailout=False,
        ),
        polish=PolishMetrics(
            enabled=True, input_count=95, output_count=90,
            cues_merged=5, cues_extended=12,
        ),
    )
    persisted = to_jsonable(original)
    revived = _pm_from_dict(persisted)
    assert revived is not None
    assert revived.audio_prep.source_channels == 6
    assert revived.audio_prep.used_center_channel is True
    assert revived.anti_hallucination.blacklist_dropped == 3
    assert revived.anti_hallucination.safety_bailout is False
    assert revived.polish.cues_merged == 5
    assert revived.polish.cues_extended == 12


def test_pm_from_dict_tolerates_missing_new_fields():
    """Pre-0.8.1 entries on disk don't carry audio_prep / polish /
    anti_hallucination — _pm_from_dict must leave them as None
    instead of raising."""
    from app.transcript_cache import _pm_from_dict

    legacy_payload = {
        "vocal_isolation": None,
        "vad": None,
        "packing": None,
        "whisper": {"cue_drop_degenerate_timestamp_count": 4},
        "translation": None,
    }
    revived = _pm_from_dict(legacy_payload)
    assert revived is not None
    assert revived.audio_prep is None
    assert revived.anti_hallucination is None
    assert revived.polish is None
    assert revived.whisper is not None
    assert revived.whisper.cue_drop_degenerate_timestamp_count == 4


def test_pm_from_dict_rehydrates_nested_refine_block():
    """WhisperMetrics.refine is a nested dataclass — _pm_from_dict
    must re-coerce it back to a RefineMetrics so the template's
    attribute access (wh.refine.buckets_evaluated) works."""
    from app.transcript_cache import _pm_from_dict

    payload = {
        "whisper": {
            "cue_drop_degenerate_timestamp_count": 0,
            "hallucinations_dropped": 0,
            "refine": {
                "buckets_evaluated": 12,
                "buckets_weak": 3,
                "buckets_refined": 2,
                "cues_added": 7,
                "cues_replaced": 1,
                "audio_seconds_refined": 1200.0,
                "skipped_reason": None,
            },
        },
    }
    revived = _pm_from_dict(payload)
    assert revived.whisper.refine is not None
    # Attribute access — would fail if refine were still a dict on
    # a dataclass field (which is the bug this test pins).
    assert revived.whisper.refine.buckets_evaluated == 12
    assert revived.whisper.refine.buckets_refined == 2
    assert revived.whisper.refine.cues_added == 7
