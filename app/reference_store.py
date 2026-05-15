"""Persistence for user-uploaded reference subtitles and their
computed comparison scores.

Layout under ``cache_dir/refs/``:

    <cache_key>.ref.srt         # the uploaded reference (verbatim)
    <cache_key>.ref.json        # the computed ReferenceScore record

The reference file is stored verbatim (no transcoding, no
normalization) so a future re-compute can rebuild the score from the
exact same input — important for reproducibility once we start
comparing scores across runs.

The score is cached so the stats page loads instantly without
re-parsing both files on every render. Re-compute is triggered
explicitly via ``recompute_score`` (called from the re-polish path
and the upload endpoint).

Two-key cache invalidation: if the user re-polishes a VTT
(``app.pipeline.polish.polish_vtt_text``), the VTT changes but the
reference doesn't. The persisted ``vtt_fingerprint`` field on the
score record lets the API endpoint detect "VTT has changed since
the score was computed" and trigger a recompute without the user
having to re-upload the reference.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app.config import settings
from app.reference import (
    ReferenceScore, compute_reference_score, detect_language,
    parse_subtitle, to_jsonable,
)
from app.util import atomic_write, load_json_with_quarantine


_log = logging.getLogger("subtitle_this")


def _refs_dir() -> Path:
    """Return the directory we put per-key reference files in,
    creating it if needed. Reads settings.cache_dir each call so
    test fixtures that swap cache_dir work without restart."""
    d = Path(settings.cache_dir) / "refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ref_path(cache_key: str) -> Path:
    return _refs_dir() / f"{cache_key}.ref.srt"


def _score_path(cache_key: str) -> Path:
    return _refs_dir() / f"{cache_key}.ref.json"


def _vtt_fingerprint(vtt_text: str) -> str:
    """Short content hash of the generated VTT. Stored on the score
    record so we can detect when the VTT changed under the reference
    (e.g. a re-polish) and recompute lazily on the next access."""
    return hashlib.sha256(vtt_text.encode("utf-8")).hexdigest()[:16]


# ── Reference upload + delete ──────────────────────────────────────────────


class LanguageMismatch(Exception):
    """Raised when an uploaded reference's detected language doesn't
    match the generated VTT's language. The API layer converts this
    to a 400 with a clear message."""


class UnreadableReference(Exception):
    """Raised when the uploaded file can't be parsed as SRT or VTT.
    The API layer converts this to a 400."""


def store_reference(
    cache_key: str,
    ref_content: str,
    generated_vtt: str,
    *,
    vtt_target_lang: str,
) -> ReferenceScore:
    """Validate the reference, compute the score, persist both.

    Validation:
    - Reference must parse to at least one cue (else ``UnreadableReference``).
    - Detected language on the reference must equal ``vtt_target_lang``
      (else ``LanguageMismatch``). Strict per-design: scoring across
      languages would give nonsensical text-similarity numbers.

    Returns the computed ``ReferenceScore`` so the API can return it
    in the upload response.
    """
    ref_cues = parse_subtitle(ref_content)
    if not ref_cues:
        raise UnreadableReference(
            "Reference file has no parseable cues — is it a valid SRT or VTT?"
        )
    ref_lang = detect_language(ref_cues)
    if ref_lang is None:
        raise LanguageMismatch(
            "Could not confidently detect the reference's language. "
            "Strict same-language policy: upload a reference that is "
            f"in {vtt_target_lang!r} (matching the generated VTT)."
        )
    if ref_lang != vtt_target_lang:
        raise LanguageMismatch(
            f"Reference is in {ref_lang!r} but the generated VTT is in "
            f"{vtt_target_lang!r}. Strict same-language policy is enabled — "
            "upload a reference matching the VTT's target language."
        )

    # All-clear. Compute + persist.
    score = compute_reference_score(generated_vtt, ref_content, lang=vtt_target_lang)
    score_dict = to_jsonable(score)
    score_dict["vtt_fingerprint"] = _vtt_fingerprint(generated_vtt)

    atomic_write(_ref_path(cache_key), ref_content)
    atomic_write(_score_path(cache_key), json.dumps(score_dict, indent=2))
    _log.info(
        "reference: stored for cache_key=%s (lang=%s, %d ref cues, "
        "overall score %d)",
        cache_key, vtt_target_lang, score.reference_count,
        score.overall_score,
    )
    return score


def delete_reference(cache_key: str) -> bool:
    """Remove the persisted reference + cached score for a cache key.
    Returns True iff at least one file existed. Best-effort — never
    raises (the API layer wants a clean 204 even if the files were
    already gone, e.g. concurrent delete)."""
    removed_any = False
    for path in (_ref_path(cache_key), _score_path(cache_key)):
        try:
            if path.exists():
                path.unlink()
                removed_any = True
        except OSError as e:
            _log.warning("reference: failed to delete %s (%s)", path, e)
    return removed_any


# ── Score lookup + recompute ──────────────────────────────────────────────


def load_score(cache_key: str) -> dict[str, Any] | None:
    """Return the cached score record as a JSON-safe dict, or None
    if there's no reference for this key. Includes the
    ``vtt_fingerprint`` field so callers can decide whether to
    recompute against a newer VTT."""
    path = _score_path(cache_key)
    return load_json_with_quarantine(path, _log, label="reference_store")


def load_reference_content(cache_key: str) -> str | None:
    """Return the uploaded reference as plain text, or None if missing."""
    path = _ref_path(cache_key)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        _log.warning("reference: failed to read %s (%s)", path, e)
        return None


def recompute_score(
    cache_key: str,
    generated_vtt: str,
    *,
    vtt_target_lang: str,
) -> ReferenceScore | None:
    """Re-run the comparison against the cached reference. Used after
    a re-polish changes the VTT — the reference is unchanged but the
    score may shift. Returns None if there's no reference for this
    key.

    Skips the language detection step (the original upload already
    validated it) — re-checking it would either be redundant or
    create a confusing failure mode if the heuristic gives a slightly
    different answer on second read.
    """
    ref_content = load_reference_content(cache_key)
    if ref_content is None:
        return None
    score = compute_reference_score(generated_vtt, ref_content, lang=vtt_target_lang)
    score_dict = to_jsonable(score)
    score_dict["vtt_fingerprint"] = _vtt_fingerprint(generated_vtt)
    atomic_write(_score_path(cache_key), json.dumps(score_dict, indent=2))
    return score


def maybe_recompute_score(
    cache_key: str,
    generated_vtt: str,
    *,
    vtt_target_lang: str,
) -> dict[str, Any] | None:
    """Lazy variant for the stats page: returns the cached score if
    its ``vtt_fingerprint`` matches the current VTT, otherwise
    recomputes and returns the fresh record. Returns None if no
    reference is on file."""
    cached = load_score(cache_key)
    if cached is None:
        return None
    current_fp = _vtt_fingerprint(generated_vtt)
    if cached.get("vtt_fingerprint") == current_fp:
        return cached
    # VTT has drifted under the reference — recompute.
    score = recompute_score(
        cache_key, generated_vtt, vtt_target_lang=vtt_target_lang,
    )
    if score is None:
        return None
    out = to_jsonable(score)
    out["vtt_fingerprint"] = current_fp
    return out
