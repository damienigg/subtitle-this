"""Post-STT filter that drops Whisper's signature hallucinations.

Whisper-large was trained on a large chunk of internet audio (including
YouTube), so when fed audio with long silent stretches it falls back to
the statistically-likely cues it saw at the END of training-data
videos: ``"Thanks for watching."``, ``"Subscribe."``, ``"Subtitles by
the Amara.org community"``, ``"Please like and subscribe."``, etc.
These hallucinations:

- Don't correspond to any audio in the source — they're pure model
  output triggered by silence + a low-confidence final decode.
- Tend to repeat across windows when ``condition_on_previous_text=True``
  (we now disable that in the faster-whisper backend; this module is
  the second line of defense).
- Look professional enough that an uninformed viewer wouldn't
  immediately spot them — which makes them more damaging to perceived
  subtitle quality than obvious garbage would be.

Filter pipeline (in order):

1. **Blacklist match.** Each cue's text is normalized
   (lowercased, punctuation stripped, whitespace collapsed) and
   matched against a list of known signature phrases. Match → drop.

2. **Repetition trim.** A cue whose normalized text contains the same
   n-gram (n ≥ 1 word) repeated ≥ 3 times in a row is dropped. This
   catches ``"yeah yeah yeah yeah yeah"`` and ``"please please
   please"``-style stuck-loop outputs.

3. **Renumber.** Dropping cues leaves gaps in the id sequence, which
   trips downstream code (cache key, polish, translate) that assumes
   contiguous ids. After filtering, ids are renumbered 0..N-1.

The filter is conservative — it never modifies cue text, only drops
whole cues. False positives (real dialogue that happens to match a
blacklisted phrase) are vanishingly rare in practice because the
matches are exact normalized strings, not substring searches.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from app.pipeline.stt import Cue


_log = logging.getLogger("subtitle_this")


# Signature phrases Whisper generates on silence + ambient audio. Each
# entry is compared after normalization (lowercase, strip punctuation,
# collapse whitespace). Add to this list when you spot a new family —
# the YouTube-corpus tail is large and a handful of new phrases surface
# per Whisper version. To keep additions safe, only add EXACT phrases
# you've personally seen Whisper produce on silence — substring matches
# would risk dropping real dialogue (e.g. "thank you" can be a real
# line; we only block the whole-cue "thank you." hallucination).
_BLACKLIST_PHRASES = frozenset({
    # The YouTube classics
    "thanks for watching",
    "thank you for watching",
    "thanks for watching this video",
    "thank you for watching this video",
    "please like and subscribe",
    "like and subscribe",
    "dont forget to like and subscribe",
    "subscribe",
    "subscribe to my channel",
    "see you in the next video",
    "see you next time",
    "see you next video",
    "see you guys later",
    "see you later",

    # Captioner credits Whisper learned to fake
    "subtitles by the amaraorg community",
    "subtitles by amaraorg community",
    "transcribed by",
    "translated by",
    "captions by",
    "subtitles by",

    # Common silence-trigger lines (the ones Whisper produces in the
    # most-silent passages — careful here, "thank you" is a real
    # dialogue line; we only block the standalone single-utterance
    # cue, which would never appear in real dialog without context).
    "thank you",
    "thank you very much",
    "thank you so much",
    "you",       # Whisper's pure noise output; appears as a one-word cue
    "the end",
    "applause",
    "music",

    # French equivalents — Whisper hallucinates in target language too
    "merci",
    "merci beaucoup",
    "merci davoir regarde",
    "abonnez vous",
    "sous titres par",

    # Spanish
    "gracias",
    "gracias por ver",
    "suscribete",

    # German
    "danke fürs zuschauen",
    "abonniert",
})


@dataclass
class FilterStats:
    """Telemetry from one anti-hallucination pass. Plumbed into
    ``pipeline_metrics`` so the stats page can show how many cues we
    cleaned up per run."""
    input_count: int = 0
    blacklisted: int = 0
    repetition_dropped: int = 0
    output_count: int = 0


# Compiled once — normalization runs per cue.
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, strip diacritics + punctuation, collapse whitespace.
    Used for blacklist comparison and n-gram repetition detection.
    Aggressive: ``"Merci d'avoir regardé !"`` → ``"merci davoir regarde"``.

    Punctuation is REPLACED WITH NOTHING (not whitespace) so that
    apostrophes within words ("d'avoir" → "davoir", "can't" → "cant")
    keep the word as a single token for blacklist matching. The
    downside — adjacent punctuation-separated words without spaces
    (``"hi.there"``) collapse to ``"hithere"`` — is harmless for our
    blacklist (which has no such entries) and for n-gram repetition
    detection (which counts tokens, not punctuation)."""
    # NFKD then drop combining marks → strips accents while keeping
    # the base letter (é → e). Cleanest cross-language way to make
    # the blacklist work for non-English titles.
    no_accents = "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )
    lower = no_accents.lower()
    no_punct = _PUNCT_RE.sub("", lower)
    return _WS_RE.sub(" ", no_punct).strip()


def _has_repetition(text: str, *, min_repeats: int = 3) -> bool:
    """True if the normalized text contains the same n-gram repeated
    ``min_repeats`` or more times consecutively.

    Examples of hits:
        "yeah yeah yeah yeah yeah" → True (1-gram × 5)
        "no no no no" → True (1-gram × 4)
        "stop it stop it stop it" → True (2-gram × 3)

    Examples of misses:
        "hello there hello again" → False (not consecutive)
        "i said it twice it twice" → False (each repeat only ×2 in row)
        "ok" → False
    """
    tokens = _normalize(text).split()
    if len(tokens) < min_repeats:
        return False

    # Check each possible n-gram length from 1 up to len(tokens) //
    # min_repeats. For min_repeats=3 and 10 tokens, that's lengths 1..3
    # (a 4-token n-gram × 3 needs 12 tokens). Tiny loop.
    max_n = len(tokens) // min_repeats
    for n in range(1, max_n + 1):
        # Slide a window of size n along the token list. For each start
        # position, check if the next ``min_repeats - 1`` n-grams are
        # identical. If any do, it's a hit.
        for start in range(0, len(tokens) - n * min_repeats + 1):
            first = tokens[start:start + n]
            ok = True
            for k in range(1, min_repeats):
                next_ngram = tokens[start + k * n : start + (k + 1) * n]
                if next_ngram != first:
                    ok = False
                    break
            if ok:
                return True
    return False


def filter_cues(cues: list[Cue]) -> tuple[list[Cue], FilterStats]:
    """Apply the full anti-hallucination pass. Returns
    ``(filtered_cues, stats)``. Cue ids in the output are renumbered
    starting at 0 so downstream code that assumes contiguous ids
    (cache key, polish, translate) keeps working.

    Idempotent: running it twice produces the same output as once
    (the blacklist + repetition rules are pure functions of text).

    **Safety net**: if the filter would drop EVERY cue (>90% blacklist
    matches → 0 survivors), we suspect the heuristic is wrong for
    this track (e.g. an actual YouTube screen-grab where the whole
    audio IS "Thanks for watching"-flavoured, or a track where
    Whisper hallucinated everywhere). In that case we return the
    original cue list with a warning logged. Better to ship the cues
    and let the user review than to fail the whole job with a
    misleading NoSpeech. The threshold is tuned so legitimate dialog
    films (where genuine YT-corpus drop is < 5% of cues) still get
    the filtered output."""
    stats = FilterStats(input_count=len(cues))
    out: list[Cue] = []
    for cue in cues:
        normalized = _normalize(cue.text)
        if normalized in _BLACKLIST_PHRASES:
            stats.blacklisted += 1
            _log.debug("anti-hallucination: dropped blacklisted cue %r", cue.text)
            continue
        if _has_repetition(cue.text):
            stats.repetition_dropped += 1
            _log.debug("anti-hallucination: dropped repetitive cue %r", cue.text)
            continue
        out.append(cue)

    # Safety: if the filter would drop everything (or near-everything
    # on a non-trivial input), bail out and return the originals.
    # The 90% threshold + minimum-size guard prevents this kicking in
    # on tiny test inputs while still catching real degenerate runs.
    total_dropped = stats.blacklisted + stats.repetition_dropped
    if len(cues) >= 10 and total_dropped >= int(0.9 * len(cues)):
        _log.warning(
            "anti-hallucination: would drop %d/%d cues (>= 90%%) — "
            "suspect the heuristic is wrong for this track. Returning "
            "ORIGINAL cue list and letting the user review. This is "
            "either a YT-screen-grab source where the dialog IS those "
            "phrases, or Whisper hallucinated across the whole audio.",
            total_dropped, len(cues),
        )
        # Preserve original cue list, but record what we WOULD have
        # done in stats so the metrics page surfaces it.
        stats.output_count = len(cues)
        return list(cues), stats

    # Renumber so ids are contiguous 0..N-1. Preserve timing + text.
    renumbered = [
        Cue(id=i, start=c.start, end=c.end, text=c.text)
        for i, c in enumerate(out)
    ]
    stats.output_count = len(renumbered)
    if stats.blacklisted or stats.repetition_dropped:
        _log.info(
            "anti-hallucination: dropped %d cue(s) (%d blacklist + %d "
            "repetition) out of %d input.",
            total_dropped, stats.blacklisted, stats.repetition_dropped,
            stats.input_count,
        )
    return renumbered, stats
