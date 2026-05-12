"""Tests for app/pipeline/polish.py — readability post-processing.

The polish pass converts Whisper's tight per-utterance timing into
the durations a viewer can actually read. The rules are subtle
(don't overlap the next cue; don't move starts; merge only when
visually-one), so each invariant gets its own focused test.
"""
from __future__ import annotations

import pytest

from app.pipeline.polish import polish_cues
from app.pipeline.stt import Cue


@pytest.fixture(autouse=True)
def _polish_defaults(monkeypatch):
    """Lock the polish-related settings to the documented defaults so
    a setting bump in config.py doesn't silently break test
    assumptions. Each test that needs a different value monkeypatches
    on top of this fixture's baseline."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "_overrides",
        {
            **settings._overrides,
            "polish_enabled": True,
            "min_cue_duration_seconds": 1.2,
            "min_seconds_per_char": 0.045,
            "merge_adjacent_cues": True,
            "max_gap_to_merge_seconds": 0.3,
            "max_merged_cue_duration_seconds": 7.0,
            "cue_separation_seconds": 0.05,
            "max_line_chars": 42,
            "max_lines_per_cue": 2,
        },
    )


def _cue(start: float, end: float, text: str, i: int = 0) -> Cue:
    return Cue(id=i, start=start, end=end, text=text)


# ── Disabled = no-op ─────────────────────────────────────────────────────


def test_polish_disabled_passes_cues_through_unchanged(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(
        settings, "_overrides",
        {**settings._overrides, "polish_enabled": False},
    )
    cues = [_cue(0.0, 0.3, "Yes.")]
    out = polish_cues(cues)
    # Same identity, no mutation, no copy round-trip.
    assert out is cues


def test_polish_does_not_mutate_input():
    """The pass returns a NEW list with NEW Cue objects so the cache
    layer (which holds a reference to the original transcription's
    cues) can serialize the pre-polish version separately."""
    cues = [_cue(0.0, 0.3, "Yes.")]
    polished = polish_cues(cues)
    assert polished is not cues
    assert polished[0] is not cues[0]
    assert cues[0].end == 0.3   # input untouched


# ── Extend pass ──────────────────────────────────────────────────────────


def test_short_cue_is_extended_to_min_duration():
    """A 0.3 s cue gets extended to 1.2 s (the absolute floor) when
    no next cue blocks the extension."""
    cues = [_cue(10.0, 10.3, "Yes.")]
    out = polish_cues(cues)
    assert out[0].start == 10.0   # start never moves
    assert abs(out[0].end - 11.2) < 0.001


def test_long_cue_is_left_alone():
    """A cue that already meets the readability floor passes through
    untouched."""
    cues = [_cue(10.0, 13.5, "A longer sentence.")]
    out = polish_cues(cues)
    assert out[0].end == 13.5


def test_extend_is_capped_by_next_cue_start(monkeypatch):
    """The extend pass must never produce overlapping cues. When the
    next cue starts soon, the current cue can only extend to that
    start minus cue_separation_seconds — even if it leaves the cue
    below the readability minimum.

    Merge is disabled here so the cap-by-next-cue path is the only
    thing under test (otherwise the two short adjacent cues would
    just collapse into one and bypass the cap entirely)."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "_overrides",
        {**settings._overrides, "merge_adjacent_cues": False},
    )
    cues = [
        _cue(10.0, 10.3, "Yes."),
        _cue(10.6, 11.5, "No."),
    ]
    out = polish_cues(cues)
    # First cue's end is capped at next.start - 0.05 = 10.55.
    assert abs(out[0].end - 10.55) < 0.001, (
        "Extended cue overlaps the next one — cue_separation invariant broken"
    )


def test_extend_uses_reading_speed_for_long_text():
    """A 40-char cue needs 40 × 0.045 = 1.8 s of display time, more
    than the absolute 1.2 s floor. The character-based minimum wins."""
    text = "x" * 40   # exactly 40 chars
    cues = [_cue(10.0, 10.5, text)]
    out = polish_cues(cues)
    assert abs(out[0].end - (10.0 + 40 * 0.045)) < 0.001


def test_extend_never_moves_start():
    """The cue's start aligns with the audio onset — moving it would
    desync the subtitle from what the viewer hears. The extend pass
    only ever pushes ``end`` forward."""
    cues = [_cue(7.5, 7.6, "Hi")]
    out = polish_cues(cues)
    assert out[0].start == 7.5


# ── Merge pass ───────────────────────────────────────────────────────────


def test_merge_two_short_adjacent_cues():
    """Two cues with a tiny gap and short combined text collapse into
    one. Preserves the earlier start and the later end; text joined
    by a single space."""
    cues = [
        _cue(10.0, 10.4, "Yes,"),
        _cue(10.5, 10.9, "of course."),   # 0.1 s gap
    ]
    out = polish_cues(cues)
    assert len(out) == 1
    assert out[0].start == 10.0
    assert out[0].end == 10.9 or out[0].end >= 10.9   # extend may push further
    assert out[0].text == "Yes, of course."


def test_merge_respects_max_gap():
    """Gap > max_gap_to_merge_seconds (default 0.3 s) → no merge."""
    cues = [
        _cue(10.0, 10.4, "Yes."),
        _cue(11.0, 11.5, "Indeed."),   # 0.6 s gap
    ]
    out = polish_cues(cues)
    assert len(out) == 2


def test_merge_respects_max_total_chars():
    """Combined text exceeding ``max_line_chars × max_lines_per_cue``
    (default 42 × 2 = 84) is too long to fit on screen — no merge."""
    cues = [
        _cue(10.0, 10.5, "x" * 50),
        _cue(10.6, 11.0, "y" * 50),   # combined would be 101 chars
    ]
    out = polish_cues(cues)
    assert len(out) == 2


def test_merge_respects_max_merged_duration():
    """Combined display span exceeding the per-cue duration ceiling
    is treated as too cluttered for one subtitle — no merge."""
    cues = [
        _cue(0.0, 0.5, "Start."),
        _cue(0.7, 8.0, "End."),   # combined span 8 s > 7 s default
    ]
    out = polish_cues(cues)
    assert len(out) == 2


def test_merge_can_chain_three_or_more():
    """Greedy merging: a run of 'Yes.' / 'Yes.' / 'Yes.' collapses
    into one cue, not two."""
    cues = [
        _cue(10.0, 10.4, "Yes."),
        _cue(10.5, 10.9, "Yes."),
        _cue(11.0, 11.4, "Yes."),
    ]
    out = polish_cues(cues)
    assert len(out) == 1
    assert out[0].text == "Yes. Yes. Yes."


def test_merge_disabled_keeps_cues_separate(monkeypatch):
    """Operators can disable just the merge pass while keeping the
    extend pass active."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "_overrides",
        {**settings._overrides, "merge_adjacent_cues": False},
    )
    cues = [
        _cue(10.0, 10.4, "Yes,"),
        _cue(10.5, 10.9, "of course."),
    ]
    out = polish_cues(cues)
    assert len(out) == 2


# ── Ordering / ids ───────────────────────────────────────────────────────


def test_ids_are_resequenced_after_merge():
    """Merges drop entries; the returned list re-numbers IDs from 0
    so downstream consumers (the .vtt writer, the cache) see a
    contiguous sequence."""
    cues = [
        _cue(10.0, 10.4, "A,", i=0),
        _cue(10.5, 10.9, "B.", i=1),
        _cue(20.0, 21.0, "C.", i=2),
    ]
    out = polish_cues(cues)
    assert [c.id for c in out] == list(range(len(out)))


def test_empty_input_returns_empty():
    assert polish_cues([]) == []
