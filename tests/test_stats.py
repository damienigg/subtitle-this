"""Tests for app/stats.py — subtitle quality / coverage metrics.

The module is the single source of truth for what the Cache Explorer's
stats page (and the .stats.json sidecar) reports. Behaviors the tests
lock in:

- Cue parsing: VTT timestamp format → seconds, multi-line cue text
  joined with a space, WEBVTT / NOTE preamble + blank separators
  skipped, no cues yields an empty stats record (no crashes).
- Duration buckets: a heavy <0.5s tail is detected and surfaced via
  very_short_pct (the Inception regression signal — Whisper-
  compressed timestamps).
- Coverage buckets: the 10-min slicing covers [0, last_cue_end] and
  flat distributions stay flat (no silent collapse onto the first
  bucket — that was the v0.6.0-0.7.1 STT bug).
- NOTE-header parsing surfaces lang / mode / provider / whisper for
  entries whose cached payload predates the media_path field.
- Sidecar write is atomic and tolerates a failed mkdir without
  blowing up the calling job (we never want a stats-write failure
  to fail the actual subtitle generation).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import stats as stats_mod


def _vtt(*cues: tuple[str, str, str], note: str | None = None) -> str:
    """Build a synthetic .vtt from (start, end, text) triples. start /
    end strings use the WEBVTT format HH:MM:SS.mmm. Tests use this to
    fabricate deterministic inputs without hand-writing the file."""
    parts = ["WEBVTT", ""]
    if note:
        parts.append(f"NOTE {note}")
        parts.append("")
    for s, e, text in cues:
        parts.append(f"{s} --> {e}")
        parts.append(text)
        parts.append("")
    return "\n".join(parts)


# ── Cue parsing ────────────────────────────────────────────────────────────


def test_compute_from_empty_vtt_returns_zero_stats():
    """An empty (header-only) .vtt mustn't crash — the field is just
    "no cues to report" rather than an exception."""
    s = stats_mod.compute_from_vtt(_vtt())
    assert s.cue_count == 0
    assert s.total_display_seconds == 0.0
    assert s.coverage_buckets == []
    assert s.very_short_pct == 0.0


def test_compute_from_one_cue_extracts_basic_stats():
    s = stats_mod.compute_from_vtt(_vtt(
        ("00:00:10.000", "00:00:12.500", "Hello world"),
    ))
    assert s.cue_count == 1
    assert s.total_display_seconds == 2.5
    assert s.avg_duration_seconds == 2.5
    assert s.min_duration_seconds == 2.5
    assert s.max_duration_seconds == 2.5
    assert s.total_characters == len("Hello world")
    assert s.last_cue_end_seconds == 12.5
    # On-screen 2.5 s out of 12.5 s of "runtime" = 20 %.
    assert s.speech_display_ratio_pct == 20.0


def test_compute_handles_multi_line_cue_text():
    """Pro-subtitled cues wrap onto two lines. The character count must
    reflect the readable string (with the line break replaced by a single
    space), not include the literal '\\n'."""
    s = stats_mod.compute_from_vtt(_vtt(
        ("00:00:00.000", "00:00:02.000", "line one\nline two"),
    ))
    assert s.total_characters == len("line one line two")


# ── Duration buckets ───────────────────────────────────────────────────────


def test_duration_buckets_classify_each_band():
    """One cue per bucket — each bucket increments exactly once,
    no off-by-one at the band edges (the <0.5 band is exclusive at 0.5)."""
    cues = [
        ("00:00:00.000", "00:00:00.300", "very short"),   # 0.3 s → <0.5
        ("00:00:01.000", "00:00:01.800", "shortish"),      # 0.8 s → 0.5-1
        ("00:00:02.000", "00:00:03.500", "medium"),        # 1.5 s → 1-2
        ("00:00:04.000", "00:00:07.500", "long"),          # 3.5 s → 2-5
        ("00:00:08.000", "00:00:15.000", "very long"),     # 7.0 s → >5
    ]
    s = stats_mod.compute_from_vtt(_vtt(*cues))
    b = s.duration_buckets
    assert (b.lt_0_5, b.lt_1_0, b.lt_2_0, b.lt_5_0, b.gte_5_0) == (1, 1, 1, 1, 1)


def test_very_short_pct_surfaces_compressed_timestamps_pattern():
    """The Inception-style regression had 28.6 % of cues under 0.5 s
    because Whisper was emitting compressed timestamps. very_short_pct
    is the metric we point at to spot the pattern."""
    cues = []
    # 3 short + 1 normal = 75 % "very short"
    for i in range(3):
        cues.append((f"00:00:{i:02d}.000", f"00:00:{i:02d}.200", "blip"))
    cues.append(("00:00:10.000", "00:00:12.000", "normal cue"))

    s = stats_mod.compute_from_vtt(_vtt(*cues))

    assert s.duration_buckets.lt_0_5 == 3
    assert s.very_short_pct == 75.0


# ── Coverage buckets ───────────────────────────────────────────────────────


def test_coverage_buckets_span_full_timeline():
    """Cues across 25 minutes of audio produce 3 buckets (0-10, 10-20,
    20-30), each containing the cues that *start* in that 10-min
    window. The flat-distribution case is what we want to confirm —
    the v0.6.0 regression collapsed everything onto bucket 0, this
    test would have caught that immediately."""
    cues = [
        ("00:01:00.000", "00:01:02.000", "first 10 min"),
        ("00:08:00.000", "00:08:02.000", "first 10 min"),
        ("00:15:00.000", "00:15:02.000", "second 10 min"),
        ("00:24:00.000", "00:24:02.000", "third 10 min, just barely"),
    ]
    s = stats_mod.compute_from_vtt(_vtt(*cues))

    assert len(s.coverage_buckets) == 3
    assert [b.cue_count for b in s.coverage_buckets] == [2, 1, 1]
    assert s.coverage_buckets[0].start_min == 0
    assert s.coverage_buckets[2].end_min == 30


def test_coverage_buckets_flag_zone_with_no_cues():
    """A bucket at 0 surrounded by populated buckets is the "VAD
    rejected this scene" signature. The test fixes the count so the
    UI's gap-finding stays correct."""
    cues = [
        ("00:01:00.000", "00:01:02.000", "early"),
        ("00:21:00.000", "00:21:02.000", "late"),
    ]
    s = stats_mod.compute_from_vtt(_vtt(*cues))

    assert [b.cue_count for b in s.coverage_buckets] == [1, 0, 1]


# ── NOTE-header parsing ────────────────────────────────────────────────────


def test_note_header_populates_metadata_fields():
    """Legacy cache entries don't carry media_path. The NOTE line is
    the only place lang/mode/provider/whisper are recoverable from."""
    s = stats_mod.compute_from_vtt(
        _vtt(
            ("00:00:01.000", "00:00:02.000", "hi"),
            note="Subtitle This auto-subs (en -> fr, mode=audio, "
                 "whisper=large-v3-turbo, provider=nllb)",
        )
    )
    assert s.source_lang == "en"
    assert s.target_lang == "fr"
    assert s.mode == "audio"
    assert s.whisper_model == "large-v3-turbo"
    assert s.provider == "nllb"


def test_note_header_polished_marker_is_captured():
    """The 0.7.20 polish marker in the NOTE line surfaces as
    ``stats.polished == True``. Absence (legacy entries) leaves
    ``polished`` at None so the UI can render "unknown" rather than
    misreport as raw."""
    s = stats_mod.compute_from_vtt(
        _vtt(
            ("00:00:01.000", "00:00:02.000", "hi"),
            note="Subtitle This auto-subs (en -> fr, mode=audio, "
                 "whisper=small, provider=nllb, polished=true)",
        )
    )
    assert s.polished is True


def test_note_header_without_polished_marker_returns_none():
    s = stats_mod.compute_from_vtt(
        _vtt(
            ("00:00:01.000", "00:00:02.000", "hi"),
            note="Subtitle This auto-subs (en -> fr, mode=audio, "
                 "whisper=small, provider=nllb)",
        )
    )
    assert s.polished is None


def test_passed_in_overrides_take_precedence_over_note_parsing():
    """When the caller knows mode/detected_lang/etc. authoritatively
    (e.g. straight off a live job), those values shouldn't be silently
    overwritten by the NOTE-header parse if both are present."""
    s = stats_mod.compute_from_vtt(
        _vtt(
            ("00:00:01.000", "00:00:02.000", "hi"),
            note="Subtitle This auto-subs (en -> fr, mode=scene, "
                 "whisper=small, provider=nllb)",
        ),
        mode="cinematic",   # caller knows the real mode
    )
    assert s.mode == "cinematic"


# ── Sidecar write ──────────────────────────────────────────────────────────


def test_write_sidecar_produces_atomic_json_file(tmp_path):
    """The sidecar should land at {vtt}.stats.json with valid JSON
    and an intermediate .tmp must not survive a successful write."""
    vtt_path = tmp_path / "movie.fr.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")
    stats = stats_mod.compute_from_vtt(_vtt(
        ("00:00:00.000", "00:00:02.000", "hi"),
    ))

    stats_mod.write_sidecar(vtt_path, stats)

    side = Path(str(vtt_path) + ".stats.json")
    assert side.exists()
    data = json.loads(side.read_text())
    assert data["cue_count"] == 1
    assert data["schema_version"] == "1"
    # No leftover .tmp file.
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_cache_sidecar_lives_inside_cache_dir(tmp_path, monkeypatch):
    """The new sidecar lives under cache_dir/stats/, not next to the .vtt
    in the user's movie folder. Path is keyed by the VTT cache_key so
    the explorer can pair them trivially."""
    from app.config import settings as runtime_settings
    # Strip any stale instance attribute left over from another test
    # that used the legacy ``settings.cache_dir = X`` pattern (which
    # monkeypatch restores AS an instance attr, shadowing _overrides).
    if "cache_dir" in runtime_settings.__dict__:
        monkeypatch.delattr(runtime_settings, "cache_dir", raising=False)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": str(cache_dir)},
    )
    stats = stats_mod.compute_from_vtt(_vtt(
        ("00:00:00.000", "00:00:02.000", "hi"),
    ))

    stats_mod.write_cache_sidecar("abc123def4567890", stats)

    expected = cache_dir / "stats" / "abc123def4567890.json"
    assert expected.exists()
    # Movie folder is untouched — no .stats.json sitting in tmp_path
    # at the top level.
    assert list(tmp_path.glob("*.stats.json")) == []


def test_delete_cache_sidecar_removes_file(tmp_path, monkeypatch):
    from app.config import settings as runtime_settings
    # Strip any stale instance attribute left over from another test
    # that used the legacy ``settings.cache_dir = X`` pattern (which
    # monkeypatch restores AS an instance attr, shadowing _overrides).
    if "cache_dir" in runtime_settings.__dict__:
        monkeypatch.delattr(runtime_settings, "cache_dir", raising=False)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": str(cache_dir)},
    )
    stats_mod.write_cache_sidecar("delme00000", stats_mod.compute_from_vtt(_vtt()))
    assert (cache_dir / "stats" / "delme00000.json").exists()

    assert stats_mod.delete_cache_sidecar("delme00000") is True
    assert not (cache_dir / "stats" / "delme00000.json").exists()

    # Idempotent — deleting again returns False, doesn't raise.
    assert stats_mod.delete_cache_sidecar("delme00000") is False


def test_write_sidecar_swallows_oserror(tmp_path, caplog):
    """A failed disk write must NOT raise — a metrics write that
    failed should never take down the surrounding job. Confirm
    silence on the caller side and a single WARNING in the log."""
    import logging
    caplog.set_level(logging.WARNING, logger="subtitle_this")
    # Target a path under a file (which isn't a dir) — mkdir(parents)
    # will fail and so will the write.
    bad_parent = tmp_path / "blocker"
    bad_parent.write_text("not a directory")
    vtt_path = bad_parent / "inner" / "movie.vtt"
    stats = stats_mod.compute_from_vtt(_vtt())

    # Must not raise.
    stats_mod.write_sidecar(vtt_path, stats)

    assert any("stats sidecar write failed" in r.message for r in caplog.records)
