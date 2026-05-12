"""Heuristic quality score for a finished subtitle run.

Builds a single 0-100 number (and a 0-5 star rendering of it) from the
stats record + pipeline metrics so an operator can answer the
"should I trust this output?" question at a glance, without
cross-referencing six different histograms.

Important caveats — these are PROXIES for quality, not measurements of
correctness:

- We have no reference subtitle to compare against, so we can't detect
  mistranslations or missing dialog from "missing" in the absolute sense.
- The score detects PIPELINE PATHOLOGIES — things we know damage the
  output (compressed timestamps, pad-zone drops, empty translations,
  VAD under-detection). A run with no detected pathologies gets a high
  score because there's no red flag, not because we verified the text.

What this means for the user: a 95+ score means "no obvious problems" —
manual spot-check still warranted on the first run of an unfamiliar
film. A 60- score means "the pipeline mis-behaved in known ways" — go
look at the breakdown on the stats page and re-tune.

Penalty thresholds were picked from the Inception post-mortem where
each pathology produced clearly diagnostic numbers (28.6 % very-short
cues, ~50 % drop in cue density vs reference, etc.).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class QualityFactor:
    """One contributor to the quality score. Surfaced to the UI as a
    list so the user sees WHY the score is what it is, not just a
    number — the breakdown is the actionable part."""
    name: str           # short label, e.g. "Compressed timestamps"
    severity: str       # "info" | "warn" | "critical" — colors the row
    penalty: int        # points removed from the base 100
    detail: str         # one-line explanation in plain language


@dataclass
class QualityScore:
    """Composite output. ``score`` is the 0-100 value; ``stars`` is
    ``round(score/20)`` (0-5); ``grade`` follows the US academic A-F
    convention because three letters spans ~half the user base's
    intuition for "rough quality bucket"."""
    score: int = 100
    stars: int = 5
    grade: str = "A"
    summary: str = ""
    factors: list[QualityFactor] = field(default_factory=list)


def _grade_for(score: int) -> str:
    """Map 0-100 → letter. The bands are slightly more lenient than US
    grading (A starts at 90, F starts at 50) because a 65 here usually
    still means "watchable subtitle with some artefacts" rather than
    "failing", and the user shouldn't see F for a usable output."""
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 45:
        return "D"
    return "F"


def _summary_for(score: int, factors: list[QualityFactor]) -> str:
    """One-line headline rendered above the breakdown table. Phrased
    so the operator can tell at a glance whether to spot-check the
    output (high) or to tune the pipeline and re-run (low)."""
    critical = sum(1 for f in factors if f.severity == "critical")
    warn = sum(1 for f in factors if f.severity == "warn")
    if score >= 90 and not critical and not warn:
        return "Clean run — no pipeline pathologies detected."
    if score >= 75:
        return "Mostly clean — minor issues, output probably usable."
    if score >= 60:
        return "Some pathologies present — spot-check the output before trusting it."
    if score >= 45:
        return "Multiple pathologies — likely missing or garbled content. Re-tune and re-run."
    return "Severe pipeline issues — output likely unusable. See the breakdown."


def compute_quality_score(stats: "Any") -> QualityScore:
    """Inputs:
    - a VttStats record (dataclass from app.stats, or any object with
      the same attribute shape — duck-typed here so tests can pass a
      lightweight stub).

    The function looks at:
    - .very_short_pct, .cue_count (.vtt-derived — always present)
    - .pipeline_metrics["vad"] (optional, OpenVINO STT runs only)
    - .pipeline_metrics["packing"] (optional)
    - .pipeline_metrics["whisper"] (optional)
    - .pipeline_metrics["translation"] (optional, all providers)

    Missing sub-records simply skip the related checks — older cache
    entries from before the corresponding instrumentation shipped get
    fewer factors evaluated, not a wrong score.
    """
    factors: list[QualityFactor] = []
    score = 100

    pm = getattr(stats, "pipeline_metrics", None) or {}
    if not isinstance(pm, dict):
        pm = {}

    # ── (1) Compressed timestamps (the very-short-cue signal) ──────────
    very_short_pct = getattr(stats, "very_short_pct", 0.0) or 0.0
    if very_short_pct > 25:
        factors.append(QualityFactor(
            name="Compressed timestamps",
            severity="critical",
            penalty=15,
            detail=f"{very_short_pct:.1f} % of cues are under 0.5 s — "
                   "Whisper is emitting compressed timestamps "
                   "(typical of turbo on packed windows).",
        ))
        score -= 15
    elif very_short_pct > 15:
        factors.append(QualityFactor(
            name="Compressed timestamps",
            severity="warn",
            penalty=8,
            detail=f"{very_short_pct:.1f} % of cues are under 0.5 s "
                   "(threshold for concern: > 15 %).",
        ))
        score -= 8

    # ── (2) Region-packing pad-drops ──────────────────────────────────
    packing = pm.get("packing") if isinstance(pm.get("packing"), dict) else None
    if packing:
        drop = int(packing.get("cue_drop_pad_zone_count") or 0)
        keep = int(packing.get("cue_keep_count") or 0)
        total = drop + keep
        if total > 0:
            drop_pct = 100.0 * drop / total
            if drop_pct > 20:
                factors.append(QualityFactor(
                    name="Region-packing pad-drops",
                    severity="critical",
                    penalty=20,
                    detail=f"{drop_pct:.1f} % of decoded cues fell in silence "
                           "pads and were silently dropped. Turn off "
                           "stt_region_packing for a definitive fix.",
                ))
                score -= 20
            elif drop_pct > 10:
                factors.append(QualityFactor(
                    name="Region-packing pad-drops",
                    severity="warn",
                    penalty=10,
                    detail=f"{drop_pct:.1f} % of decoded cues dropped to "
                           "pad zones (threshold: > 10 %).",
                ))
                score -= 10
            elif drop_pct > 5:
                factors.append(QualityFactor(
                    name="Region-packing pad-drops",
                    severity="info",
                    penalty=5,
                    detail=f"{drop_pct:.1f} % of cues dropped to pad zones — "
                           "moderate, mostly tolerable.",
                ))
                score -= 5

    # ── (3) VAD under-detection (speech ratio too low) ────────────────
    vad = pm.get("vad") if isinstance(pm.get("vad"), dict) else None
    if vad:
        speech_ratio = float(vad.get("speech_ratio_pct") or 0.0)
        audio_s = float(vad.get("total_audio_seconds") or 0.0)
        # Only judge speech_ratio if we have meaningful audio (else
        # the ratio is just noise from a tiny test clip).
        if audio_s > 60 and 0 < speech_ratio < 20:
            factors.append(QualityFactor(
                name="VAD under-detection",
                severity="critical",
                penalty=15,
                detail=f"Silero detected speech in only {speech_ratio:.1f} % "
                       "of the audio. On a dialog-heavy film this usually "
                       "means VAD threshold is too strict — drop "
                       "vad threshold from 0.5 to 0.3.",
            ))
            score -= 15
        elif audio_s > 60 and 20 <= speech_ratio < 30:
            factors.append(QualityFactor(
                name="VAD low coverage",
                severity="warn",
                penalty=5,
                detail=f"Speech ratio {speech_ratio:.1f} % — on the low side "
                       "for a dialog film (expected 30-55 %).",
            ))
            score -= 5

        # VAD trimming syllables (lots of barely-passed regions)
        short_region_pct = float(vad.get("short_region_pct") or 0.0)
        if short_region_pct > 40:
            factors.append(QualityFactor(
                name="VAD trimming short words",
                severity="warn",
                penalty=10,
                detail=f"{short_region_pct:.1f} % of VAD regions are under "
                       "0.5 s — the 'barely-passed' zone. Suggests "
                       "VAD is rejecting short words / syllables.",
            ))
            score -= 10
        elif short_region_pct > 25:
            factors.append(QualityFactor(
                name="VAD trimming short words",
                severity="info",
                penalty=5,
                detail=f"{short_region_pct:.1f} % short regions (mild).",
            ))
            score -= 5

    # ── (4) Whisper degenerate-timestamp drops ────────────────────────
    whisper = pm.get("whisper") if isinstance(pm.get("whisper"), dict) else None
    if whisper:
        degen = int(whisper.get("cue_drop_degenerate_timestamp_count") or 0)
        cue_count = int(getattr(stats, "cue_count", 0) or 0)
        # Compare to the final cue count — if Whisper hallucinated/
        # collapsed N cues for every 100 kept ones, that's a signal.
        if cue_count > 0:
            degen_per_100 = 100.0 * degen / cue_count
            if degen_per_100 > 20:
                factors.append(QualityFactor(
                    name="Whisper hallucinations",
                    severity="warn",
                    penalty=10,
                    detail=f"{degen} cues dropped with end ≤ start "
                           f"({degen_per_100:.1f} per 100 kept). High "
                           "rate corroborates timestamp compression.",
                ))
                score -= 10
            elif degen_per_100 > 5:
                factors.append(QualityFactor(
                    name="Whisper hallucinations",
                    severity="info",
                    penalty=3,
                    detail=f"{degen} degenerate-timestamp drops "
                           f"({degen_per_100:.1f} per 100 cues).",
                ))
                score -= 3

    # ── (5) Translation empty / duplicate ─────────────────────────────
    translation = pm.get("translation") if isinstance(pm.get("translation"), dict) else None
    if translation:
        out_count = int(translation.get("output_cue_count") or 0)
        empty = int(translation.get("empty_output_count") or 0)
        dup = int(translation.get("duplicate_output_count") or 0)
        if out_count > 0:
            empty_pct = 100.0 * empty / out_count
            if empty_pct > 10:
                factors.append(QualityFactor(
                    name="Empty translations",
                    severity="critical",
                    penalty=25,
                    detail=f"{empty_pct:.1f} % of cues translated to "
                           "empty strings. NLLB int8 quantization is "
                           "the typical culprit — set "
                           "nllb_load_in_8bit = false.",
                ))
                score -= 25
            elif empty_pct > 5:
                factors.append(QualityFactor(
                    name="Empty translations",
                    severity="warn",
                    penalty=12,
                    detail=f"{empty_pct:.1f} % empty translations.",
                ))
                score -= 12

            dup_pct = 100.0 * dup / out_count
            if dup_pct > 30:
                factors.append(QualityFactor(
                    name="Duplicate translations",
                    severity="warn",
                    penalty=15,
                    detail=f"{dup_pct:.1f} % of cues share a "
                           "translation with another cue. High rate "
                           "suggests model collapse.",
                ))
                score -= 15
            elif dup_pct > 15:
                factors.append(QualityFactor(
                    name="Duplicate translations",
                    severity="info",
                    penalty=7,
                    detail=f"{dup_pct:.1f} % duplicate translations "
                           "(mild — short reactions naturally repeat).",
                ))
                score -= 7

        # Input/output cue mismatch — shouldn't happen with current
        # providers, but if it does it's a strong signal.
        in_count = int(translation.get("input_cue_count") or 0)
        if in_count > 0 and out_count > 0 and in_count != out_count:
            ratio = abs(in_count - out_count) / in_count
            if ratio > 0.05:
                factors.append(QualityFactor(
                    name="Cue count mismatch",
                    severity="critical",
                    penalty=15,
                    detail=f"Provider returned {out_count} cues for "
                           f"{in_count} inputs — content was added or "
                           "merged.",
                ))
                score -= 15

    # Final clamp + outputs
    score = max(0, score)
    stars = round(score / 20)
    grade = _grade_for(score)
    summary = _summary_for(score, factors)
    return QualityScore(
        score=score, stars=stars, grade=grade, summary=summary, factors=factors,
    )


def to_jsonable(score: QualityScore) -> dict[str, Any]:
    """Flat JSON-safe dict for the stats sidecar / API response."""
    return asdict(score)
