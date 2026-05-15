"""Objective comparison of a generated VTT against a user-uploaded
reference subtitle file (pro SRT, fan SRT, professional VTT — anything
the operator considers ground truth).

Replaces the *heuristic* Quality Score (which is a pipeline-pathology
detector — "did the pipeline behave correctly") with a **real** score
calibrated against an actual reference. The two coexist on the stats
page: the heuristic tells you whether the pipeline mis-behaved; the
reference tells you whether the output is close to professional-grade.

The six dimensions, mapped to the 0.7.x–0.8.x quality improvements
they were each designed to move:

1. **Coverage** — % of reference cues that have a corresponding cue
   in the generated VTT within ±3 s. Drives improvements #1 (center-
   channel), #4 (loudnorm), #5 (refine pass), #6 (vocal isolation).
2. **Timing accuracy** — median |start_offset| on the matched pairs.
   Drives improvement #3 (word-level timestamps) and the polish pass.
3. **Density ratio** — generated_cue_count / reference_cue_count.
   Drives the polish merge pass and the anti-hallucination filter.
4. **Orphan rate** — share of cues whose last word is a function
   word (the, of, à, de, …). Drives improvement #7 (orphan breaks).
5. **Reading speed** — median chars/second across cues. Drives the
   polish extend pass.
6. **Text similarity** — character-level F1 (chrF) between matched
   pairs. Drives the translation provider choice.

The weighted overall score is 0-100. We grade A/B/C/D/F with the
same thresholds as the heuristic Quality Score for visual symmetry
on the stats page.

Language enforcement: this module deliberately does NOT compute the
score across languages. The API endpoint that uses it must verify
the reference and the generated VTT share a language BEFORE calling
``compute_reference_score`` — chrF, density, and orphan-rate are
language-dependent and meaningless across languages.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any


# ── Cue parsing ─────────────────────────────────────────────────────────────


@dataclass
class _Cue:
    """Minimal cue shape for reference comparison. Distinct from
    ``app.pipeline.stt.Cue`` so this module stays free of pipeline
    imports and the math is testable in isolation."""
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# WebVTT and SRT use almost the same timestamp grammar — SRT uses a
# comma before the millis, VTT uses a dot. Accept both so the parser
# handles either format with one regex.
_TS_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})"
)


def _ts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_subtitle(content: str) -> list[_Cue]:
    """Parse SRT or WebVTT text into a flat list of cues.

    Tolerant of:
    - WEBVTT header line + NOTE blocks (skipped)
    - SRT cue-index lines before the timestamp (skipped)
    - Multi-line cue text (joined with a single space — line breaks
      are ignored for comparison purposes; we measure visual length
      via len(text) and that's what matters)
    - BOMs and stray Windows line endings

    Returns cues sorted by start time. Degenerate cues (end ≤ start)
    are dropped silently — pro subtitles don't have them, and an
    operator who uploads a malformed file would only get noise from
    them anyway.
    """
    if content.startswith("﻿"):
        content = content[1:]
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    cues: list[_Cue] = []
    for block in content.split("\n\n"):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        if not lines:
            continue
        # Skip the WEBVTT header and NOTE blocks. NOTE may be the
        # first line ("NOTE ...") or a standalone "NOTE" line followed
        # by a body — either way nothing after a NOTE is a cue.
        if lines[0].upper().startswith("WEBVTT") or lines[0].startswith("NOTE"):
            continue
        # Find the timestamp line; an SRT cue starts with the index,
        # so the timestamp may be lines[0] or lines[1].
        ts_idx = None
        for i, ln in enumerate(lines[:2]):
            if _TS_RE.search(ln):
                ts_idx = i
                break
        if ts_idx is None:
            continue
        m = _TS_RE.search(lines[ts_idx])
        if not m:
            continue
        start = _ts_to_seconds(*m.group(1, 2, 3, 4))
        end = _ts_to_seconds(*m.group(5, 6, 7, 8))
        text = " ".join(lines[ts_idx + 1:]).strip()
        if text and end > start:
            cues.append(_Cue(start=start, end=end, text=text))
    cues.sort(key=lambda c: c.start)
    return cues


# ── Language detection (no external dep) ────────────────────────────────────


# Stop-word lists by ISO 639-1 code. The detector counts how many of
# each list appear in the first ~500 normalized tokens of the input;
# the highest-count list wins. Words chosen to be HIGH-FREQUENCY in
# subtitle dialogue specifically — pronouns, prepositions, copulas —
# which means even very short SRTs (300 cues) give the detector
# enough signal. Picked from manual cross-checking against typical
# Whisper output on real films.
_LANG_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset({
        "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
        "for", "with", "from", "by", "is", "are", "was", "were", "be",
        "you", "your", "i", "me", "my", "we", "they", "he", "she", "it",
        "this", "that", "these", "those", "what", "who", "where", "when",
        "have", "has", "had", "do", "does", "did", "not", "no", "yes",
    }),
    "fr": frozenset({
        "le", "la", "les", "un", "une", "des", "de", "du", "et", "ou", "mais",
        "que", "qui", "quoi", "ce", "cette", "ces", "mon", "ma", "mes",
        "ton", "ta", "tes", "son", "sa", "ses", "je", "tu", "il", "elle",
        "nous", "vous", "ils", "elles", "est", "es", "sont", "etre", "avoir",
        "a", "au", "aux", "pour", "par", "avec", "sans", "dans", "sur",
        "ne", "pas", "plus", "oui", "non", "alors", "si",
    }),
    "es": frozenset({
        "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
        "y", "o", "pero", "que", "se", "no", "es", "esta", "estos", "estas",
        "te", "me", "le", "lo", "mi", "tu", "su", "para", "por", "con",
        "sin", "en", "soy", "eres", "son", "ha", "he", "yo", "tu", "el",
        "ella", "nosotros", "ustedes", "ellos", "si", "como", "muy",
    }),
    "de": frozenset({
        "der", "die", "das", "ein", "eine", "und", "oder", "aber", "ist",
        "sind", "war", "waren", "sein", "hat", "haben", "ich", "du", "er",
        "sie", "wir", "ihr", "mich", "dich", "ihn", "uns", "euch", "mein",
        "dein", "sein", "ihre", "nicht", "kein", "in", "an", "auf", "mit",
        "zu", "von", "fur", "ja", "nein",
    }),
    "it": frozenset({
        "il", "la", "lo", "i", "gli", "le", "un", "una", "e", "o", "ma",
        "che", "di", "da", "in", "con", "su", "per", "tra", "fra",
        "io", "tu", "lui", "lei", "noi", "voi", "loro", "sono", "sei",
        "ho", "ha", "avere", "essere", "non", "si", "no",
    }),
}


_TOKEN_RE = re.compile(r"\b[\w']+\b", re.UNICODE)


def _normalize_for_lang_detect(text: str) -> list[str]:
    """Lowercase + drop accents + tokenize. Drop-accents lets ``être``
    match the entry ``etre`` in the FR stop-word list (we strip accents
    in the list as well for the same reason)."""
    no_accents = "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )
    return _TOKEN_RE.findall(no_accents.lower())


def detect_language(cues: list[_Cue], *, max_tokens: int = 500) -> str | None:
    """Return the ISO 639-1 code with the strongest stop-word signal,
    or None if the input is too short / ambiguous.

    Pure-Python, no dependency on a model. Accurate enough for subtitle
    files (200+ cues across a feature film easily clear the ambiguity
    threshold) and 100 % offline. The threshold is "winning lang has
    at least 5 stop-words AND outscores runner-up by 2×" — anything
    less ambiguous is reported as None and the caller treats it as
    "couldn't detect, refuse upload".
    """
    tokens: list[str] = []
    for cue in cues:
        tokens.extend(_normalize_for_lang_detect(cue.text))
        if len(tokens) >= max_tokens:
            break
    if not tokens:
        return None
    counts: dict[str, int] = {
        lang: sum(1 for t in tokens if t in stopwords)
        for lang, stopwords in _LANG_STOPWORDS.items()
    }
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top_lang, top_count = ranked[0]
    if top_count < 5:
        return None
    runner_up_count = ranked[1][1] if len(ranked) > 1 else 0
    if runner_up_count > 0 and top_count < 2 * runner_up_count:
        return None
    return top_lang


# ── Cue matching ───────────────────────────────────────────────────────────


def _match_cues(
    generated: list[_Cue],
    reference: list[_Cue],
    *,
    window_seconds: float = 3.0,
) -> list[tuple[_Cue | None, _Cue | None]]:
    """Greedy two-pointer alignment of generated vs reference cues.

    Returns a list of pairs ``(gen, ref)`` where one side may be None
    (= unmatched). A reference cue is paired with the FIRST generated
    cue whose start falls within ``±window_seconds`` of it that hasn't
    been claimed yet by an earlier reference cue. Generated cues that
    never get claimed appear as ``(gen, None)`` (= extras).

    This is intentionally simple (O(n+m)) — it's not a globally-optimal
    alignment, but in practice subtitle drift is mostly monotonic and
    the greedy pass produces matches indistinguishable from Hungarian
    for our purposes. Pathological cases (e.g. a Whisper run that
    desynced by 30 s mid-film) would benefit from an iterative
    re-alignment, but those are also pipeline pathologies that the
    heuristic Quality Score would flag separately.
    """
    pairs: list[tuple[_Cue | None, _Cue | None]] = []
    i = 0  # generated index
    j = 0  # reference index
    n = len(generated)
    m = len(reference)
    while i < n and j < m:
        gen = generated[i]
        ref = reference[j]
        delta = gen.start - ref.start
        if abs(delta) <= window_seconds:
            pairs.append((gen, ref))
            i += 1
            j += 1
        elif delta < 0:
            # Generated cue is too early — extra cue with no ref match.
            pairs.append((gen, None))
            i += 1
        else:
            # Reference cue is too early — generated missed it.
            pairs.append((None, ref))
            j += 1
    while i < n:
        pairs.append((generated[i], None))
        i += 1
    while j < m:
        pairs.append((None, reference[j]))
        j += 1
    return pairs


# ── Orphan-rate helper ─────────────────────────────────────────────────────


# Function words that pro subtitlers avoid ending a line on (orphan
# words). Same lists as ``app/pipeline/vtt.py`` but duplicated here
# deliberately — keeping the reference module pipeline-free so it can
# be tested in isolation and so a future change to the pipeline list
# doesn't accidentally shift the reference score's calibration.
_ORPHAN_TAIL_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset({
        "a", "an", "the", "of", "in", "on", "at", "for", "to", "with",
        "and", "or", "but", "if", "as", "by", "from",
    }),
    "fr": frozenset({
        "le", "la", "les", "un", "une", "des", "de", "du", "et", "ou",
        "que", "qui", "à", "au", "aux", "en", "dans", "sur", "pour",
        "par", "avec", "sans",
    }),
    "es": frozenset({
        "el", "la", "los", "las", "un", "una", "unos", "unas", "de",
        "del", "y", "o", "que", "a", "en", "con", "por", "para",
    }),
    "de": frozenset({
        "der", "die", "das", "ein", "eine", "und", "oder", "in", "an",
        "auf", "mit", "zu", "von", "fur",
    }),
    "it": frozenset({
        "il", "la", "lo", "i", "gli", "le", "un", "una", "e", "o", "che",
        "di", "da", "in", "con", "su", "per", "tra", "fra",
    }),
}


def _orphan_rate(cues: list[_Cue], lang: str) -> float:
    """Fraction of cues whose last word is a function word (orphan).
    Pro subtitle conventions avoid this entirely (~0 %); raw Whisper
    output is typically 5-12 %. Improvement #7 (orphan-line-breaks
    in the VTT writer) targets this directly."""
    if not cues:
        return 0.0
    orphans = _ORPHAN_TAIL_WORDS.get(lang, frozenset())
    if not orphans:
        return 0.0
    hits = 0
    for cue in cues:
        # Last word = last token after a space or newline. Strip
        # trailing punctuation since ``of.`` is still an orphan.
        last_line = cue.text.splitlines()[-1] if cue.text else ""
        words = re.findall(r"[\w']+", last_line.lower())
        if words and words[-1] in orphans:
            hits += 1
    return round(100.0 * hits / len(cues), 1)


# ── chrF text similarity ──────────────────────────────────────────────────


def _chrf(hypothesis: str, reference: str, *, n: int = 6, beta: float = 2.0) -> float:
    """Character-level F-beta with n-gram order ``n``. Range [0, 1].

    Why chrF over BLEU for subtitles:
    - Robust on very short sentences (one-cue cues are common in
      subs; BLEU's brevity penalty over-penalizes them).
    - Language-agnostic (operates on chars, not whitespace-split
      tokens — works equally well for FR / EN / DE / JA).
    - chrF is the de-facto standard for MT evaluation alongside
      BLEU since the chrF++ paper (Popovic 2017).

    Beta=2 weights recall slightly higher than precision (matching
    the chrF2 variant most papers report), which fits the subtitle
    use-case: missing words from the reference is worse than adding
    words to it.
    """
    if not hypothesis or not reference:
        return 0.0
    # Drop spaces — chrF traditionally operates on the printable
    # character sequence, not whitespace. Keeps results stable when
    # one side uses different cue-internal line breaks.
    hyp = hypothesis.replace(" ", "")
    ref = reference.replace(" ", "")
    if not hyp or not ref:
        return 0.0
    precisions: list[float] = []
    recalls: list[float] = []
    for k in range(1, n + 1):
        if len(hyp) < k or len(ref) < k:
            continue
        hyp_ngrams = Counter(hyp[i:i + k] for i in range(len(hyp) - k + 1))
        ref_ngrams = Counter(ref[i:i + k] for i in range(len(ref) - k + 1))
        # Overlap = sum of mins per n-gram.
        overlap = sum((hyp_ngrams & ref_ngrams).values())
        if overlap == 0:
            precisions.append(0.0)
            recalls.append(0.0)
            continue
        precisions.append(overlap / sum(hyp_ngrams.values()))
        recalls.append(overlap / sum(ref_ngrams.values()))
    if not precisions or not recalls:
        return 0.0
    p = sum(precisions) / len(precisions)
    r = sum(recalls) / len(recalls)
    if p + r == 0:
        return 0.0
    beta_sq = beta * beta
    return round((1 + beta_sq) * p * r / (beta_sq * p + r), 3)


# ── Reading-speed helper ──────────────────────────────────────────────────


def _reading_speed_cps(cues: list[_Cue]) -> float:
    """Median chars-per-second across cues. Pro subtitles sit around
    15-18 cps (BBC guideline: ≤ 17 cps; Netflix: ≤ 20 cps adult);
    raw Whisper often pushes 25+ cps on fast utterances because cues
    are too short to read. Improvement: the polish extend pass keeps
    this in check."""
    if not cues:
        return 0.0
    speeds = [
        len(c.text) / c.duration
        for c in cues
        if c.duration > 0 and c.text
    ]
    if not speeds:
        return 0.0
    speeds.sort()
    mid = len(speeds) // 2
    if len(speeds) % 2:
        return round(speeds[mid], 1)
    return round((speeds[mid - 1] + speeds[mid]) / 2.0, 1)


# ── Score dataclass + weighted aggregator ────────────────────────────────


@dataclass
class ReferenceScore:
    """The computed reference-comparison record. All fields except
    ``overall_score`` / ``overall_grade`` are dimension measurements;
    the overall is the weighted aggregate.

    Dimension targets (where "100 / 100" lands):
    - coverage_pct: 100 % of reference cues matched
    - timing_offset_median_ms: 0 ms median; degrades from there
    - density_ratio: 1.0 (same cue count as reference)
    - orphan_rate_diff_pct: 0 (matches reference's orphan rate)
    - reading_speed_diff_cps: 0 (matches reference's reading speed)
    - text_similarity_chrf: 1.0 (perfect chrF)
    """
    # Coverage
    coverage_pct: float = 0.0
    matched_count: int = 0
    reference_count: int = 0
    extras_count: int = 0
    # Timing
    timing_offset_median_ms: float = 0.0
    timing_offset_p90_ms: float = 0.0
    # Density
    density_ratio: float = 0.0
    generated_count: int = 0
    # Orphan rate
    orphan_rate_pct: float = 0.0
    reference_orphan_rate_pct: float = 0.0
    # Reading speed
    reading_speed_cps: float = 0.0
    reference_reading_speed_cps: float = 0.0
    # Text similarity (mean chrF over matched pairs)
    text_similarity_chrf: float = 0.0
    matched_pairs_evaluated: int = 0
    # Overall
    overall_score: int = 0
    overall_grade: str = "F"
    # Per-dimension contributions to the overall score, for the UI
    # to show "where did the points go". Keys: coverage, timing,
    # density, orphan, reading_speed, text_similarity.
    dimension_scores: dict[str, int] = field(default_factory=dict)
    # Free-text caveat surfaced on the UI (e.g. "fewer than 100 cues
    # in the reference — score is noisier than usual").
    caveat: str | None = None


# Weights MUST sum to 100. Tuned so coverage + text similarity (the
# two dimensions that map to "did we capture the dialogue + did we
# translate it well") dominate, while the readability dimensions
# (orphan / reading speed) are tie-breakers.
_WEIGHTS: dict[str, int] = {
    "coverage": 30,
    "timing": 20,
    "density": 10,
    "orphan": 10,
    "reading_speed": 10,
    "text_similarity": 20,
}


def _score_coverage(pct: float) -> int:
    """0 % → 0 pts. 100 % → 100 pts. Pro reference subs typically have
    ~95 % coverage from a properly-tuned Whisper run, so this scale
    is roughly linear in the realistic range."""
    return max(0, min(100, int(round(pct))))


def _score_timing(median_ms: float) -> int:
    """0 ms → 100 pts. Degrades to 0 at 2000 ms median offset (= the
    cue is on the wrong scene). Word-level DTW timings on the cpu
    backend should land in the 100-300 ms range = 85-95 pts."""
    if median_ms <= 0:
        return 100
    if median_ms >= 2000:
        return 0
    return max(0, min(100, int(round(100 - (median_ms / 20)))))


def _score_density(ratio: float) -> int:
    """1.0 → 100 pts. Symmetric falloff to 0 pts at 0.5 or 2.0 (= half
    or double the reference cue count). Polish merge + extend should
    bring most runs into 0.85-1.15."""
    if ratio <= 0:
        return 0
    distance = abs(ratio - 1.0)
    if distance >= 1.0:
        return 0
    return max(0, min(100, int(round((1.0 - distance) * 100))))


def _score_orphan(generated_rate: float, reference_rate: float) -> int:
    """0 pts when the generated rate is > 15 pp worse than the
    reference; 100 pts when at or below the reference rate. The
    improvement #7 (orphan-word-breaks) targets this metric."""
    diff = max(0.0, generated_rate - reference_rate)
    if diff >= 15:
        return 0
    return max(0, min(100, int(round(100 - (diff * 100 / 15)))))


def _score_reading_speed(generated_cps: float, reference_cps: float) -> int:
    """Distance from the reference's reading speed, in cps. 0 cps
    delta → 100 pts; 10 cps delta → 0 pts (= the subtitle is either
    too fast to read or too slow / verbose)."""
    delta = abs(generated_cps - reference_cps)
    if delta >= 10:
        return 0
    return max(0, min(100, int(round(100 - (delta * 10)))))


def _score_chrf(chrf: float) -> int:
    """0.0 → 0 pts; 1.0 → 100 pts. Pro NLLB on a common language pair
    sits around 0.55-0.70 chrF vs a human reference, so this scale
    is interpretable: ~60-70 pts = "good NLLB run", 85+ pts =
    "indistinguishable from human translation"."""
    return max(0, min(100, int(round(chrf * 100))))


def _grade_for_score(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def compute_reference_score(
    generated_vtt: str,
    reference_text: str,
    *,
    lang: str,
) -> ReferenceScore:
    """The top-level entry point. Parses both inputs, aligns cues,
    computes the six dimensions, returns a ``ReferenceScore``.

    ``lang`` is the ISO 639-1 of BOTH inputs — the API layer must
    verify they match BEFORE calling this. The orphan-rate + chrF
    metrics are computed assuming both sides are in this language."""
    gen = parse_subtitle(generated_vtt)
    ref = parse_subtitle(reference_text)
    result = ReferenceScore(
        generated_count=len(gen),
        reference_count=len(ref),
    )
    if not ref:
        result.caveat = "Reference has no parseable cues."
        return result
    if not gen:
        result.caveat = "Generated VTT has no parseable cues."
        return result
    if len(ref) < 100:
        result.caveat = (
            f"Reference has only {len(ref)} cues — score is noisier "
            "than usual; consider a longer reference for a feature film."
        )

    pairs = _match_cues(gen, ref)
    matched = [(g, r) for g, r in pairs if g is not None and r is not None]
    extras = [g for g, r in pairs if g is not None and r is None]
    result.matched_count = len(matched)
    result.extras_count = len(extras)

    # ── Coverage ────────────────────────────────────────────────────
    result.coverage_pct = round(100.0 * len(matched) / len(ref), 1)

    # ── Timing accuracy ────────────────────────────────────────────
    if matched:
        offsets_ms = sorted(
            int(round(abs(g.start - r.start) * 1000)) for g, r in matched
        )
        mid = len(offsets_ms) // 2
        if len(offsets_ms) % 2:
            result.timing_offset_median_ms = float(offsets_ms[mid])
        else:
            result.timing_offset_median_ms = float(
                (offsets_ms[mid - 1] + offsets_ms[mid]) / 2
            )
        p90_idx = int(0.9 * (len(offsets_ms) - 1))
        result.timing_offset_p90_ms = float(offsets_ms[p90_idx])

    # ── Density ─────────────────────────────────────────────────────
    result.density_ratio = round(len(gen) / len(ref), 2)

    # ── Orphan rate ─────────────────────────────────────────────────
    result.orphan_rate_pct = _orphan_rate(gen, lang)
    result.reference_orphan_rate_pct = _orphan_rate(ref, lang)

    # ── Reading speed ───────────────────────────────────────────────
    result.reading_speed_cps = _reading_speed_cps(gen)
    result.reference_reading_speed_cps = _reading_speed_cps(ref)

    # ── Text similarity (mean chrF on matched pairs) ────────────────
    if matched:
        chrf_scores = [_chrf(g.text, r.text) for g, r in matched]
        result.text_similarity_chrf = round(
            sum(chrf_scores) / len(chrf_scores), 3,
        )
        result.matched_pairs_evaluated = len(chrf_scores)

    # ── Weighted overall ────────────────────────────────────────────
    dims = {
        "coverage": _score_coverage(result.coverage_pct),
        "timing": _score_timing(result.timing_offset_median_ms),
        "density": _score_density(result.density_ratio),
        "orphan": _score_orphan(
            result.orphan_rate_pct, result.reference_orphan_rate_pct,
        ),
        "reading_speed": _score_reading_speed(
            result.reading_speed_cps, result.reference_reading_speed_cps,
        ),
        "text_similarity": _score_chrf(result.text_similarity_chrf),
    }
    result.dimension_scores = dims
    weighted = sum(dims[k] * _WEIGHTS[k] for k in dims) / 100
    result.overall_score = int(round(weighted))
    result.overall_grade = _grade_for_score(result.overall_score)
    return result


def to_jsonable(score: ReferenceScore) -> dict[str, Any]:
    """Flatten to a JSON-safe dict for the persistence layer and the
    /api/cache/.../reference/score endpoint."""
    return asdict(score)
