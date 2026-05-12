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


# ── polish_vtt_text: round-trip through .vtt text ──────────────────────────


def _make_vtt(*cues: tuple[str, str, str], note: str | None = None) -> str:
    """Build a synthetic .vtt for the round-trip tests."""
    parts = ["WEBVTT", ""]
    if note:
        parts.append(f"NOTE {note}")
        parts.append("")
    for s, e, text in cues:
        parts.append(f"{s} --> {e}")
        parts.append(text)
        parts.append("")
    return "\n".join(parts)


def test_polish_vtt_text_extends_short_cues_in_place():
    """A .vtt with a too-brief cue gets the same cue extended after
    the round-trip — proves the parse → polish → re-emit path
    preserves and lengthens correctly."""
    from app.pipeline.polish import polish_vtt_text
    src = _make_vtt(("00:00:10.000", "00:00:10.300", "Yes."))

    out = polish_vtt_text(src)

    # The polished output's cue end is now beyond 10.3s (extended
    # to the 1.2s floor → end ≈ 11.2s).
    assert "00:00:11.200" in out or "00:00:11.20" in out, out


def test_polish_vtt_text_preserves_note_header():
    """The NOTE Subtitle-This-auto-subs header carries provenance
    (whisper model, provider, langs) — it MUST survive the
    re-polish round-trip so downstream metadata parsers (stats
    page, Cache Explorer) still recognize the entry."""
    from app.pipeline.polish import polish_vtt_text
    src = _make_vtt(
        ("00:00:10.000", "00:00:10.300", "Yes."),
        note="Subtitle This auto-subs (en -> fr, mode=audio, "
             "whisper=large-v3-turbo, provider=nllb)",
    )

    out = polish_vtt_text(src)

    assert "Subtitle This auto-subs (en -> fr, mode=audio" in out
    assert "whisper=large-v3-turbo" in out


def test_polish_vtt_text_merges_adjacent_short_cues():
    """End-to-end merge through the .vtt text path: two short
    adjacent cues collapse, and the resulting .vtt has only one
    cue block."""
    from app.pipeline.polish import polish_vtt_text
    src = _make_vtt(
        ("00:00:10.000", "00:00:10.400", "Yes,"),
        ("00:00:10.500", "00:00:10.900", "of course."),
    )

    out = polish_vtt_text(src)

    cue_lines = [l for l in out.splitlines() if " --> " in l]
    assert len(cue_lines) == 1, cue_lines
    assert "Yes, of course." in out


def test_polish_vtt_text_is_near_idempotent():
    """Re-running the polish on an already-polished output must not
    keep mutating cues indefinitely. The second pass produces the
    same cue boundaries as the first (allowing for floating-point
    millisecond noise in the timestamp formatter)."""
    from app.pipeline.polish import polish_vtt_text
    src = _make_vtt(
        ("00:00:10.000", "00:00:10.300", "Yes."),
        ("00:01:05.000", "00:01:05.500", "Hello there."),
    )
    once = polish_vtt_text(src)
    twice = polish_vtt_text(once)
    # Same cue COUNT and same cue boundaries.
    once_ts = [l for l in once.splitlines() if " --> " in l]
    twice_ts = [l for l in twice.splitlines() if " --> " in l]
    assert once_ts == twice_ts


def test_polish_vtt_text_empty_passes_through():
    from app.pipeline.polish import polish_vtt_text
    assert polish_vtt_text("WEBVTT\n\n") == "WEBVTT\n\n"


# ── Idempotency invariants (0.7.19) ────────────────────────────────────────


def test_extend_leaves_enough_gap_to_prevent_second_pass_merge():
    """Regression for the 0.7.17/0.7.18 drift: two non-mergeable cues
    (gap 0.7 s, above the 0.3 s threshold) used to have their gap
    crushed to ``cue_separation_seconds`` (0.05 s) by the extend
    pass — which made a second polish run merge them spuriously.

    Post-fix: the extend cap leaves at least
    ``max_gap_to_merge_seconds + epsilon`` of space when merge is
    enabled, so the no-merge decision survives any number of
    re-polish passes."""
    cues = [
        _cue(10.0, 10.3, "Yes."),
        _cue(11.0, 11.5, "No."),
    ]

    out = polish_cues(cues)

    # Two cues survived (no merge on the first pass either).
    assert len(out) == 2
    # The new gap must be at least max_gap_to_merge_seconds (0.3) —
    # below that, a second pass would merge them. We use >= 0.3
    # rather than > 0.3 because the merge predicate is strict-less-
    # than: equality is safe.
    gap = out[1].start - out[0].end
    assert gap >= 0.3, (
        f"Extend pass shrunk the gap to {gap:.3f} s — below "
        f"max_gap_to_merge_seconds (0.3). Re-polish would merge "
        "these cues despite the first pass deciding not to."
    )


def test_polish_is_idempotent_when_extend_borders_max_gap():
    """The scenario that broke idempotency before 0.7.19: two cues
    with a gap just above the merge threshold, where the first cue
    is short enough that extend wants to push its end close to the
    next cue. Pre-fix, the second polish pass would merge them;
    post-fix, two passes produce the same output."""
    cues = [
        _cue(10.0, 10.3, "Yes."),
        _cue(11.0, 11.5, "No."),
    ]

    once = polish_cues(cues)
    twice = polish_cues(once)

    assert len(once) == len(twice), (
        f"Polish is not idempotent: pass 1 → {len(once)} cues, "
        f"pass 2 → {len(twice)} cues"
    )
    # Boundary-by-boundary comparison. Floating-point exact equality
    # is fine here because polish operations are simple arithmetic
    # on millisecond-rounded values.
    for a, b in zip(once, twice):
        assert abs(a.start - b.start) < 0.001
        assert abs(a.end - b.end) < 0.001
        assert a.text == b.text


def test_polish_three_passes_converges_immediately():
    """Stronger guarantee: not just that pass-2 matches pass-1, but
    that pass-N for any N matches pass-1. Catches a class of
    "converges in 3 passes" bugs that pass-2-only assertions would
    miss."""
    cues = [
        _cue(0.0, 0.4, "Short."),
        _cue(0.6, 0.9, "Short again."),
        _cue(1.5, 1.8, "And one more."),
        _cue(10.0, 10.3, "Far away."),
        _cue(11.0, 11.5, "Just out of range."),
    ]

    pass1 = polish_cues(cues)
    pass2 = polish_cues(pass1)
    pass3 = polish_cues(pass2)

    # Pass 2 and Pass 3 must equal Pass 1 — boundary by boundary.
    for label, after in (("pass2", pass2), ("pass3", pass3)):
        assert len(after) == len(pass1), f"{label} length differs"
        for a, b in zip(pass1, after):
            assert abs(a.start - b.start) < 0.001, label
            assert abs(a.end - b.end) < 0.001, label
            assert a.text == b.text, label


def test_polished_marker_is_stamped_on_repolished_vtt():
    """polish_vtt_text appends polished=true to the NOTE header so a
    downstream viewer can tell at a glance the file went through the
    readability pass. Idempotent — re-polishing an already-marked
    .vtt leaves the marker in place (not duplicated)."""
    from app.pipeline.polish import polish_vtt_text
    src = _make_vtt(
        ("00:00:10.000", "00:00:10.300", "Yes."),
        note="Subtitle This auto-subs (en -> fr, mode=audio, "
             "whisper=large-v3-turbo, provider=nllb)",
    )

    once = polish_vtt_text(src)
    twice = polish_vtt_text(once)

    assert "polished=true" in once
    # Idempotency: only one marker, not "polished=true, polished=true".
    assert once.count("polished=true") == 1
    assert twice.count("polished=true") == 1


def test_polished_marker_preserves_other_note_fields():
    """The stamping helper inserts the marker BEFORE the closing
    parenthesis without disturbing the rest of the header — the
    Cache Explorer / stats parser still extracts the same lang /
    mode / whisper / provider."""
    from app.pipeline.polish import polish_vtt_text
    src = _make_vtt(
        ("00:00:10.000", "00:00:10.300", "Yes."),
        note="Subtitle This auto-subs (en -> fr, mode=audio, "
             "whisper=large-v3-turbo, provider=nllb)",
    )

    out = polish_vtt_text(src)

    # Note line carries every original field plus the marker.
    assert "(en -> fr, mode=audio, whisper=large-v3-turbo, provider=nllb, polished=true)" in out


def test_polish_idempotency_holds_when_merge_disabled(monkeypatch):
    """When merge_adjacent_cues is OFF the extend cap reverts to the
    conventional ``cue_separation_seconds`` (more aggressive
    extension is fine — no merge-decision to preserve). Idempotency
    must still hold in that mode."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "_overrides",
        {**settings._overrides, "merge_adjacent_cues": False},
    )
    cues = [
        _cue(10.0, 10.3, "Yes."),
        _cue(11.0, 11.5, "No."),
    ]

    once = polish_cues(cues)
    twice = polish_cues(once)

    for a, b in zip(once, twice):
        assert a.end == b.end
        assert a.start == b.start
