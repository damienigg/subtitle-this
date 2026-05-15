"""Tests for the anti-hallucination filter that drops Whisper's
signature YouTube-corpus output on silence + ambient audio."""
import pytest

from app.pipeline.anti_hallucination import (
    FilterStats, _has_repetition, _normalize, filter_cues,
)
from app.pipeline.stt import Cue


# ── _normalize ──────────────────────────────────────────────────────────────


def test_normalize_lowercases_and_strips_punctuation():
    assert _normalize("Thanks for watching!") == "thanks for watching"
    assert _normalize("Thank you, very much.") == "thank you very much"


def test_normalize_strips_accents():
    # Critical for French / Spanish / German blacklist entries to match.
    assert _normalize("Merci d'avoir regardé !") == "merci davoir regarde"
    assert _normalize("Suscríbete") == "suscribete"
    assert _normalize("Danke fürs Zuschauen") == "danke furs zuschauen"


def test_normalize_collapses_whitespace():
    assert _normalize("hello   \tworld\n") == "hello world"


# ── _has_repetition ─────────────────────────────────────────────────────────


def test_repetition_catches_single_word_loop():
    assert _has_repetition("yeah yeah yeah yeah yeah") is True
    assert _has_repetition("no no no no") is True


def test_repetition_catches_bigram_loop():
    assert _has_repetition("stop it stop it stop it") is True


def test_repetition_misses_non_consecutive():
    # Same word twice, then separated, then again — not consecutive ≥3
    assert _has_repetition("hello there hello again") is False


def test_repetition_misses_short_text():
    assert _has_repetition("ok") is False
    assert _has_repetition("hello world") is False


def test_repetition_ignores_diacritics():
    # The hallucination "OK OK OK" with mixed accents/case should match.
    assert _has_repetition("Ok ok ok ok") is True


def test_repetition_min_repeats_threshold():
    """Two consecutive matches is too few — only 3+ counts as a loop.
    Real dialogue often has 'no, no' or 'yes, yes' as natural emphasis."""
    assert _has_repetition("no no") is False
    assert _has_repetition("no, no") is False
    assert _has_repetition("no no no") is True


# ── filter_cues ─────────────────────────────────────────────────────────────


def _cue(i: int, text: str) -> Cue:
    return Cue(id=i, start=float(i), end=float(i) + 1.0, text=text)


def test_filter_drops_blacklisted_youtube_phrases():
    cues = [
        _cue(0, "Welcome to the film."),
        _cue(1, "Thanks for watching."),
        _cue(2, "Please like and subscribe!"),
        _cue(3, "Real dialogue here."),
    ]
    out, stats = filter_cues(cues)
    out_texts = [c.text for c in out]
    assert "Thanks for watching." not in out_texts
    assert "Please like and subscribe!" not in out_texts
    assert "Welcome to the film." in out_texts
    assert "Real dialogue here." in out_texts
    assert stats.blacklisted == 2


def test_filter_drops_repetition_stuck_loops():
    cues = [
        _cue(0, "Normal cue."),
        _cue(1, "yeah yeah yeah yeah yeah"),
        _cue(2, "Another normal cue."),
    ]
    out, stats = filter_cues(cues)
    out_texts = [c.text for c in out]
    assert "yeah yeah yeah yeah yeah" not in out_texts
    assert stats.repetition_dropped == 1
    assert stats.blacklisted == 0


def test_filter_renumbers_cue_ids_to_be_contiguous():
    """Downstream code (cache key, polish, translate) assumes
    ids run 0..N-1 with no gaps. Verify renumbering."""
    cues = [
        _cue(0, "First."),
        _cue(1, "Thanks for watching."),     # dropped
        _cue(2, "Third."),
        _cue(3, "Subscribe"),                 # dropped
        _cue(4, "Fifth."),
    ]
    out, _ = filter_cues(cues)
    assert [c.id for c in out] == [0, 1, 2]
    assert [c.text for c in out] == ["First.", "Third.", "Fifth."]


def test_filter_preserves_timing():
    """The ids get rewritten but start/end timestamps must come
    through unchanged — they're audio-anchored and downstream timing
    math depends on them."""
    cues = [
        _cue(0, "First cue."),
        _cue(1, "Thanks for watching."),
        _cue(2, "Third cue."),
    ]
    out, _ = filter_cues(cues)
    # First survivor is the original cue 0 → keeps its timing.
    assert (out[0].start, out[0].end) == (0.0, 1.0)
    # Second survivor is the original cue 2 → keeps its timing.
    assert (out[1].start, out[1].end) == (2.0, 3.0)


def test_filter_french_blacklist_matches_after_normalization():
    """'Merci d'avoir regardé.' must be caught by the same filter,
    via the accent-stripped 'merci davoir regarde' blacklist entry."""
    cues = [_cue(0, "Merci d'avoir regardé !")]
    out, stats = filter_cues(cues)
    assert out == []
    assert stats.blacklisted == 1


def test_filter_idempotent():
    """Running the filter twice produces the same output as once.
    Important for the cache-resume path (a previous filtered cue list
    re-entering the pipeline mustn't re-shrink)."""
    cues = [
        _cue(0, "Real dialogue."),
        _cue(1, "Thanks for watching."),
        _cue(2, "More dialogue."),
    ]
    once, _ = filter_cues(cues)
    twice, _ = filter_cues(once)
    assert [c.text for c in once] == [c.text for c in twice]


def test_filter_emits_zero_stats_on_clean_input():
    cues = [
        _cue(0, "Normal subtitle line."),
        _cue(1, "Another normal line."),
    ]
    out, stats = filter_cues(cues)
    assert len(out) == 2
    assert stats.blacklisted == 0
    assert stats.repetition_dropped == 0
    assert stats.input_count == 2
    assert stats.output_count == 2


def test_filter_empty_input():
    out, stats = filter_cues([])
    assert out == []
    assert stats == FilterStats(input_count=0, blacklisted=0, repetition_dropped=0, output_count=0)


# ── Safety net: bail out if the filter would drop everything ────────────────


def test_filter_returns_originals_when_would_drop_90_percent():
    """A degenerate run (Whisper hallucinates the whole audio, OR the
    source genuinely IS a YT-screen-grab with those phrases as real
    dialog) shouldn't lose ALL cues — better to ship them and let the
    user review. Threshold: 90% drop on a list ≥ 10 cues."""
    cues = [
        _cue(0, "Real dialogue here."),  # the only non-blacklisted
        _cue(1, "Thanks for watching."),
        _cue(2, "Subscribe"),
        _cue(3, "Like and subscribe"),
        _cue(4, "Please like and subscribe"),
        _cue(5, "Thank you for watching"),
        _cue(6, "Thanks for watching this video"),
        _cue(7, "See you next time"),
        _cue(8, "See you later"),
        _cue(9, "See you guys later"),
        _cue(10, "Subscribe to my channel"),
    ]
    # 10/11 = 90.9% would be dropped → safety net kicks in.
    out, stats = filter_cues(cues)
    # Originals returned unchanged, with their original ids.
    assert [c.id for c in out] == [c.id for c in cues]
    assert [c.text for c in out] == [c.text for c in cues]
    # Stats still record what the filter WOULD have done (input_count
    # + blacklisted), so the operator can see the heuristic fired.
    assert stats.blacklisted == 10
    assert stats.output_count == 11   # what we returned


def test_filter_below_safety_threshold_still_filters_normally():
    """A 50% drop is high but not degenerate — that's the actual
    contract (drop the hallucinations, keep the dialog). Safety net
    only triggers at ≥90%."""
    cues = [
        _cue(0, "First real line."),
        _cue(1, "Thanks for watching."),
        _cue(2, "Second real line."),
        _cue(3, "Subscribe"),
        _cue(4, "Third real line."),
        _cue(5, "Subscribe to my channel"),
        _cue(6, "Fourth real line."),
        _cue(7, "See you next time"),
        _cue(8, "Fifth real line."),
        _cue(9, "Like and subscribe"),
    ]
    out, stats = filter_cues(cues)
    # 5 hallucinations dropped, 5 reals kept.
    assert len(out) == 5
    assert stats.blacklisted == 5
    assert all("Subscribe" not in c.text for c in out)


def test_filter_safety_does_not_kick_in_on_short_input():
    """Threshold guard requires ≥10 cues — short inputs (test fixtures,
    very short clips) shouldn't trigger the safety regardless of
    blacklist density."""
    cues = [
        _cue(0, "Thanks for watching."),
        _cue(1, "Subscribe"),
    ]
    out, stats = filter_cues(cues)
    # All dropped, no safety bail-out — 2 cues is below the
    # ≥10 minimum for the heuristic to be statistically meaningful.
    assert out == []
    assert stats.blacklisted == 2
