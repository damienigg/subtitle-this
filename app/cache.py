"""Transcript cache with infallible re-keying across mtime changes.

Two fingerprints, used together:

- **quick fingerprint** — `path + size + mtime` hashed. Microsecond cost.
  The hot-path key. Misses when mtime moves (rsync touching files, our own
  mkvpropedit write-back step bumping the mtime, backup tools, etc.) or
  when the file is renamed/moved.

- **content fingerprint** — `size + 64 KB sample at 1 MB offset + 64 KB
  sample at the file midpoint`, hashed. ~10 ms cost (two short reads).
  Stable across mtime changes, path moves/renames, and metadata-only
  edits — the offset of 1 MB sits past any container header (EBML in
  Matroska, moov in MP4) so a tag-only edit doesn't shift the sampled
  bytes. Invalidates on real audio/video re-encode, replacement with a
  different file, or anything that touches the data sections.

The processor performs a two-level lookup: try the quick key first, fall
back to the content key on miss. On a content-key hit we re-link the
payload under the current quick key so future lookups are O(1) again
without re-doing the content read.

This guarantees that paid work (Whisper + LLM calls) is never repeated
just because the file's mtime moved or the file was relocated. It is
explicitly NOT meant to be cryptographically collision-resistant — both
fingerprints truncate sha256 to 16 hex chars (64 bits). For media files
of distinct content the practical collision rate is vanishingly small.
"""
import hashlib
import json
from pathlib import Path

from app.config import settings


# Sampling parameters for the content fingerprint. 1 MB head-skip is
# generous enough to clear an MKV EBML header (typically <100 KB) and an
# MP4 moov (variable, but usually <1 MB; for files where moov is at the
# end, the head sample sits in mdat which is data, not metadata).
_CONTENT_HEAD_SKIP = 1 << 20         # 1 MB
_CONTENT_SAMPLE_SIZE = 64 << 10      # 64 KB per sample
_CONTENT_MIN_FILE_SIZE = 2 * (_CONTENT_HEAD_SKIP + _CONTENT_SAMPLE_SIZE)


def quick_fingerprint(path: Path) -> str:
    """Path + size + mtime, sha256-truncated. Sub-millisecond. Hits the
    cache in the common case (file unchanged since the last run).

    Bit-for-bit compatible with the previous `file_fingerprint()` so
    existing on-disk cache entries from earlier versions still resolve."""
    st = path.stat()
    h = hashlib.sha256()
    h.update(str(path.resolve()).encode())
    h.update(str(st.st_size).encode())
    h.update(str(int(st.st_mtime)).encode())
    return h.hexdigest()[:16]




def content_fingerprint(path: Path) -> str:
    """Stable across mtime, path, and tag-only edits (mkvpropedit). Used
    as the fallback key when the quick fingerprint misses.

    Reads two 64 KB samples — one at offset 1 MB (past the container
    header), one at the file midpoint — plus the file size. Total IO
    ~128 KB. Falls back to a full-file hash for files smaller than the
    sampling threshold (rare for media but covers test fixtures and
    short audio clips).
    """
    size = path.stat().st_size
    h = hashlib.sha256()
    # Namespace the hash so it can never collide with a quick fingerprint
    # for a different file at the same hex prefix.
    h.update(b"content-v1:")
    h.update(str(size).encode())

    with path.open("rb") as f:
        if size < _CONTENT_MIN_FILE_SIZE:
            for chunk in iter(lambda: f.read(64 << 10), b""):
                h.update(chunk)
        else:
            f.seek(_CONTENT_HEAD_SKIP)
            h.update(f.read(_CONTENT_SAMPLE_SIZE))
            f.seek(size // 2)
            h.update(f.read(_CONTENT_SAMPLE_SIZE))

    return h.hexdigest()[:16]


def cache_key(
    media_fingerprint: str,
    target_lang: str,
    model: str,
    provider: str,
    source_priority: list[str],
    mode: str,
    *,
    scene_threshold: float | None = None,
    translation_llm_model: str | None = None,
    vision_llm_model: str | None = None,
) -> str:
    """Build a stable cache key. Each kwarg is included only when relevant —
    callers pass None for kwargs that don't affect the output for their request:

    - `scene_threshold`: relevant for scene/cinematic modes. Different threshold
      → different scene bible → different final VTT.
    - `translation_llm_model`: relevant when provider="llm". Different LLM model
      → different translation output. Switching the configured translation
      model (e.g. claude-opus-4-7 → gpt-4o → qwen2.5:72b) must invalidate the
      cache.
    - `vision_llm_model`: relevant for scene/cinematic modes (the bible content
      depends on which LLM described the keyframes).
    """
    parts = [
        media_fingerprint,
        target_lang,
        model,
        provider,
        ",".join(source_priority),
        mode,
    ]
    if scene_threshold is not None:
        parts.append(f"thr={scene_threshold:.3f}")
    if translation_llm_model:
        parts.append(f"tllm={translation_llm_model}")
    if vision_llm_model:
        parts.append(f"vllm={vision_llm_model}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def cache_path(key: str) -> Path:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    return settings.cache_dir / f"{key}.json"


def load(key: str) -> dict | None:
    p = cache_path(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable cache file — treat as a miss so we recompute.
        # The next store() call will overwrite it cleanly.
        return None


def store(key: str, payload: dict) -> None:
    cache_path(key).write_text(json.dumps(payload))


# ── Two-level cache helpers ───────────────────────────────────────────────────


def lookup_two_level(
    media: Path,
    *,
    target_lang: str,
    model: str,
    provider: str,
    source_priority: list[str],
    mode: str,
    scene_threshold: float | None = None,
    translation_llm_model: str | None = None,
    vision_llm_model: str | None = None,
) -> tuple[dict | None, str, str]:
    """Look up a cached payload using both fingerprints. Returns
    `(cached_payload_or_None, quick_key, content_key)`.

    On a content-key hit we re-link the payload under the current quick
    key so the next call returns from the fast path without paying for
    another content read.

    The caller stores fresh payloads via `store_two_level()` so they're
    addressable by either key going forward.
    """
    key_kwargs = dict(
        target_lang=target_lang, model=model, provider=provider,
        source_priority=source_priority, mode=mode,
        scene_threshold=scene_threshold,
        translation_llm_model=translation_llm_model,
        vision_llm_model=vision_llm_model,
    )
    quick_fp = quick_fingerprint(media)
    quick_key = cache_key(quick_fp, **key_kwargs)
    cached = load(quick_key)
    if cached is not None:
        # Don't pay the content-fingerprint read on the hot path.
        return cached, quick_key, ""

    content_fp = content_fingerprint(media)
    content_key = cache_key(content_fp, **key_kwargs)
    if content_key == quick_key:
        # Astronomically unlikely (different fingerprint inputs colliding
        # at 64 bits), but bail out cleanly if it ever happens.
        return None, quick_key, content_key

    cached = load(content_key)
    if cached is not None:
        # Re-link under the quick key so subsequent lookups skip the
        # content read.
        store(quick_key, cached)
    return cached, quick_key, content_key


def store_two_level(
    media: Path,
    payload: dict,
    *,
    target_lang: str,
    model: str,
    provider: str,
    source_priority: list[str],
    mode: str,
    scene_threshold: float | None = None,
    translation_llm_model: str | None = None,
    vision_llm_model: str | None = None,
) -> None:
    """Store the payload under both the quick and content keys so future
    lookups can hit either one."""
    key_kwargs = dict(
        target_lang=target_lang, model=model, provider=provider,
        source_priority=source_priority, mode=mode,
        scene_threshold=scene_threshold,
        translation_llm_model=translation_llm_model,
        vision_llm_model=vision_llm_model,
    )
    quick_fp = quick_fingerprint(media)
    content_fp = content_fingerprint(media)
    quick_key = cache_key(quick_fp, **key_kwargs)
    content_key = cache_key(content_fp, **key_kwargs)
    store(quick_key, payload)
    if content_key != quick_key:
        store(content_key, payload)
