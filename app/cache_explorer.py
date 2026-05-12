"""Read-side helpers for the Cache Explorer page.

This module knows how to walk the on-disk cache layout — the top-level
``.json`` files (the VTT/result cache) and the ``transcripts/`` subdir
(the intermediate STT cache) — and turn each entry into a metadata
record the UI can render. It also exposes the surgical-delete helpers
the page wires its per-row buttons to.

Two cache buckets are visible from the UI:

- **VTT cache** (top-level ``cache_dir/*.json``): one file per
  (media × language × mode × provider × …) combination. The same
  payload is stored under both the quick-fp and content-fp keys
  (two-level lookup), so a single media+config produces *two*
  entries with identical content. The explorer lists them both so
  the user knows the on-disk footprint is what it is — collapsing
  them would hide that. Delete operates per-file.
- **Transcript cache** (``cache_dir/transcripts/*.json``): the
  cached Whisper output, keyed by content_fp + whisper config.
  Lives separately so a user can force a fresh STT pass while
  keeping the VTT cache intact (rare workflow, mostly useful when
  a Whisper config change is being tested).

Out of scope here: ``hf/``, ``openvino-models/``, ``nllb-models/``,
``tmp/``, ``settings.json``, and ``jobs.json``. Those are model
weights and runtime state, not artefacts that "served to generate a
subtitle" — the page documents that distinction in its help text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import settings


# The NOTE line we write to the .vtt header is the only place where
# things like whisper_model / provider are stored together with the
# language pair. Parsing it lets us enrich entries written before the
# explorer existed (which don't carry media_path either).
#
#   NOTE Subtitle This auto-subs (en -> fr, mode=audio, whisper=large-v3-turbo, provider=nllb)
# or, with the 0.7.20 readability marker:
#   NOTE Subtitle This auto-subs (en -> fr, mode=audio, whisper=..., provider=nllb, polished=true)
_NOTE_RE = re.compile(
    r"NOTE Subtitle This auto-subs "
    r"\((?P<src>[a-z]{2}) -> (?P<tgt>[a-z]{2}), "
    r"mode=(?P<mode>[a-z]+), "
    r"whisper=(?P<whisper>[^,]+), "
    r"provider=(?P<provider>[^,)]+)"
    r"(?:, polished=(?P<polished>true|false))?"
    r"\)"
)


@dataclass
class VttEntry:
    """One row in the VTT-cache section of the Cache Explorer.

    Note on cache_keys vs cache_key: each logical entry is written
    twice on disk — under the quick-fingerprint key AND the content-
    fingerprint key — so the two-level lookup can hit either. From the
    user's perspective those two files are the same record. The UI
    dedupes them into a single row; ``cache_key`` is the "primary"
    (the one used for stats / download URLs), ``cache_keys`` carries
    the full set so a delete removes them together."""
    cache_key: str                       # filename stem of the primary file
    cache_keys: list[str] = field(default_factory=list)   # all files in this group
    media_path: str | None = None        # populated for 0.7.4+ entries
    media_name: str | None = None        # basename of media_path, for display
    source_lang: str | None = None
    target_lang: str | None = None
    mode: str | None = None
    provider: str | None = None
    whisper_model: str | None = None
    # Polish marker from the NOTE header — True / False / None.
    # None means "no marker present" (pre-0.7.20 entries) which the
    # UI surfaces as a "polish status unknown" pill so the operator
    # knows to re-polish if they want the readability pass applied.
    polished: bool | None = None
    cue_count: int | None = None
    size_bytes: int = 0                  # sum across all files in the group
    modified_at: float = 0.0             # max mtime in the group
    preview: str | None = None           # first cue's text, truncated

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class TranscriptEntry:
    """One row in the Transcript-cache section of the Cache Explorer."""
    cache_key: str
    detected_language: str | None = None
    cue_count: int = 0
    whisper_backend: str | None = None
    whisper_model: str | None = None
    vad_enabled: bool | None = None
    track_index: int | None = None
    size_bytes: int = 0
    modified_at: float = 0.0
    parsed: bool = True                  # False = filename couldn't be decoded

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# ── VTT cache ─────────────────────────────────────────────────────────────


def _parse_vtt_payload(path: Path) -> VttEntry:
    """Build a VttEntry from one cache_dir/*.json file. Defensive against
    malformed payloads — a corrupted entry still produces a row with the
    filename so the user can delete it without a backend crash."""
    import json
    entry = VttEntry(
        cache_key=path.stem,
        size_bytes=path.stat().st_size,
        modified_at=path.stat().st_mtime,
    )
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return entry   # delete-only row; user can clean it up by hand

    if not isinstance(data, dict):
        return entry

    media = data.get("media_path")
    if isinstance(media, str):
        entry.media_path = media
        entry.media_name = Path(media).name
    if isinstance(data.get("mode"), str):
        entry.mode = data["mode"]
    if isinstance(data.get("cue_count"), int):
        entry.cue_count = data["cue_count"]

    vtt = data.get("vtt") or ""
    if isinstance(vtt, str) and vtt:
        m = _NOTE_RE.search(vtt)
        if m:
            entry.source_lang = m.group("src")
            entry.target_lang = m.group("tgt")
            if entry.mode is None:
                entry.mode = m.group("mode")
            entry.whisper_model = m.group("whisper")
            entry.provider = m.group("provider")
            polished_g = m.group("polished")
            entry.polished = (polished_g == "true") if polished_g else None
        # First cue's text, truncated. Skip the WEBVTT/NOTE preamble.
        for chunk in vtt.split("\n\n"):
            lines = chunk.strip().split("\n")
            if lines and "-->" in lines[0]:
                text = " ".join(lines[1:]).strip()
                if text:
                    entry.preview = text[:80] + ("…" if len(text) > 80 else "")
                    break

    return entry


def _dedupe_key(e: VttEntry) -> tuple:
    """The "logical record" identifier — two on-disk files with the
    same dedupe key represent the same run (two-level cache writes
    each payload under both the quick-fp and content-fp keys). Used
    to collapse those into one UI row.

    ``polished`` is NOT part of the dedupe key — the same logical
    cache record can switch between polished/unpolished across
    re-polish cycles, and we want the UI to show one row whose
    polished pill flips when the marker changes."""
    return (
        e.media_path or "",
        e.source_lang or "",
        e.target_lang or "",
        e.mode or "",
        e.provider or "",
        e.whisper_model or "",
        # Fall-back fingerprint for legacy entries missing media_path
        # AND a NOTE header — preview text is the next-best identity.
        # Stays empty for fully-populated entries so dedupe is stable.
        e.preview or "" if not e.media_path else "",
    )


def list_vtt_entries() -> list[VttEntry]:
    """Walk cache_dir/*.json (NOT recursing into subdirs — transcripts/
    lives one level down and is handled separately).

    Logically deduplicated: each VTT entry is written to disk under
    two keys (quick-fp + content-fp), so a naive listing would show
    each film twice with identical metadata. We group by the
    ``_dedupe_key`` tuple and present one row per logical record;
    ``cache_keys`` carries the full set so delete removes them
    together. Newest group first."""
    root = Path(settings.cache_dir)
    if not root.is_dir():
        return []
    raw: list[VttEntry] = []
    for child in root.iterdir():
        # Top-level .json only — skip subdirs (transcripts/, etc.) and
        # the runtime overrides / jobs files. Those aren't subtitle
        # artefacts and a stray click on "Delete" mustn't nuke them.
        if not child.is_file() or child.suffix != ".json":
            continue
        if child.name in {"settings.json", "jobs.json"}:
            continue
        raw.append(_parse_vtt_payload(child))

    # Group + merge by dedupe key. Within a group: keep one entry,
    # promote all cache_keys, sum sizes, take the latest mtime.
    groups: dict[tuple, VttEntry] = {}
    for e in raw:
        key = _dedupe_key(e)
        if key not in groups:
            e.cache_keys = [e.cache_key]
            groups[key] = e
        else:
            merged = groups[key]
            merged.cache_keys.append(e.cache_key)
            merged.size_bytes += e.size_bytes
            if e.modified_at > merged.modified_at:
                merged.modified_at = e.modified_at
                # Promote the newer file's cache_key as the primary
                # so stats / download URLs hit the freshest copy if
                # the two diverged for any reason.
                merged.cache_key = e.cache_key
    out = list(groups.values())
    out.sort(key=lambda e: e.modified_at, reverse=True)
    return out


def delete_vtt_entry(cache_key: str) -> bool:
    """Delete cache_dir/{cache_key}.json. Returns True if a file was
    removed, False if nothing existed there. Raises ValueError if the
    key contains anything suspicious — defense in depth against an HTTP
    handler accidentally forwarding a path-traversal value.

    Also removes the paired stats sidecar at
    ``cache_dir/stats/{cache_key}.json`` so the two artefacts go
    away together — otherwise the explorer would list a phantom
    sidecar that points to a deleted entry."""
    _validate_cache_key(cache_key)
    path = Path(settings.cache_dir) / f"{cache_key}.json"
    # Guard against /settings.json or /jobs.json being targeted via a
    # cleverly chosen key. Belt-and-suspenders to _validate_cache_key.
    if path.name in {"settings.json", "jobs.json"}:
        raise ValueError(f"refusing to delete runtime file: {path.name!r}")
    if not path.is_file():
        return False
    path.unlink()
    # Best-effort sidecar cleanup — never fail the parent delete on
    # a missing/un-deletable sidecar.
    try:
        from app import stats as stats_mod
        stats_mod.delete_cache_sidecar(cache_key)
    except Exception:
        pass
    return True


def clear_all_vtt_entries() -> int:
    """Bulk-delete every VTT cache entry. Returns the count removed.
    Skips the runtime files (settings.json, jobs.json) that share the
    directory but aren't subtitle artefacts."""
    count = 0
    for entry in list_vtt_entries():
        if delete_vtt_entry(entry.cache_key):
            count += 1
    return count


# ── Transcript cache ──────────────────────────────────────────────────────


# Schema-v2 transcript filenames look like:
#   v2_{content_fp}_{backend}_{model_with_slashes_to_dashes}_vad{0|1}_t{track}
# Older v1 keys (no schema prefix) are still readable; we just don't
# decode their per-axis fields.
_TRANSCRIPT_KEY_RE = re.compile(
    r"^v2_(?P<content_fp>[0-9a-f]+)_(?P<backend>[a-z_]+)_"
    r"(?P<model>.+)_vad(?P<vad>[01])_t(?P<track>\d+)$"
)


def _parse_transcript_payload(path: Path) -> TranscriptEntry:
    import json
    entry = TranscriptEntry(
        cache_key=path.stem,
        size_bytes=path.stat().st_size,
        modified_at=path.stat().st_mtime,
    )
    m = _TRANSCRIPT_KEY_RE.match(path.stem)
    if m:
        entry.whisper_backend = m.group("backend")
        entry.whisper_model = m.group("model")
        entry.vad_enabled = bool(int(m.group("vad")))
        entry.track_index = int(m.group("track"))
    else:
        entry.parsed = False
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            if isinstance(data.get("detected_language"), str):
                entry.detected_language = data["detected_language"]
            cues = data.get("cues")
            if isinstance(cues, list):
                entry.cue_count = len(cues)
    except (OSError, json.JSONDecodeError):
        pass   # delete-only row
    return entry


def list_transcript_entries() -> list[TranscriptEntry]:
    out: list[TranscriptEntry] = []
    root = Path(settings.cache_dir) / "transcripts"
    if not root.is_dir():
        return out
    for child in root.iterdir():
        if not child.is_file() or child.suffix != ".json":
            continue
        out.append(_parse_transcript_payload(child))
    out.sort(key=lambda e: e.modified_at, reverse=True)
    return out


def delete_transcript_entry(cache_key: str) -> bool:
    _validate_cache_key(cache_key)
    path = Path(settings.cache_dir) / "transcripts" / f"{cache_key}.json"
    if not path.is_file():
        return False
    path.unlink()
    return True


def clear_all_transcript_entries() -> int:
    count = 0
    for entry in list_transcript_entries():
        if delete_transcript_entry(entry.cache_key):
            count += 1
    return count


# ── shared safety ─────────────────────────────────────────────────────────


# Cache keys are hex hashes (VTT entries) or the v2_… composite shown
# above (transcript entries). Both are restricted to alphanum + a small
# set of safe punctuation, with no path separators. Anything outside
# this set is rejected as a path-traversal attempt.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _validate_cache_key(cache_key: str) -> None:
    if not cache_key or not _SAFE_KEY_RE.match(cache_key):
        raise ValueError(f"invalid cache key: {cache_key!r}")
