import json
from pathlib import Path

from app import cache


def test_quick_fingerprint_stable(tmp_path):
    p = tmp_path / "movie.mkv"
    p.write_bytes(b"x" * 100)
    f1 = cache.quick_fingerprint(p)
    f2 = cache.quick_fingerprint(p)
    assert f1 == f2


def test_quick_fingerprint_changes_on_mtime(tmp_path):
    import os, time
    p = tmp_path / "movie.mkv"
    p.write_bytes(b"x")
    f1 = cache.quick_fingerprint(p)
    # Bump mtime by 1 second
    new_time = p.stat().st_mtime + 1
    os.utime(p, (new_time, new_time))
    f2 = cache.quick_fingerprint(p)
    assert f1 != f2


def test_cache_key_is_deterministic():
    args = ("fp123", "fr", "small", "llm", ["en", "ja"], "audio")
    assert cache.cache_key(*args) == cache.cache_key(*args)


def test_cache_key_includes_threshold_only_when_provided():
    base = cache.cache_key("fp", "fr", "small", "llm", ["en"], "audio")
    with_threshold = cache.cache_key("fp", "fr", "small", "llm", ["en"], "scene", scene_threshold=0.4)
    assert base != with_threshold

    same_threshold = cache.cache_key("fp", "fr", "small", "llm", ["en"], "scene", scene_threshold=0.4)
    diff_threshold = cache.cache_key("fp", "fr", "small", "llm", ["en"], "scene", scene_threshold=0.5)
    assert same_threshold != diff_threshold


def test_cache_key_distinguishes_translation_llm_models():
    """Switching the translation LLM (claude-opus → gpt-4o → qwen2.5:72b) must
    invalidate the cache so we don't serve a stale Claude translation as if it
    were the new model's output."""
    a = cache.cache_key("fp", "fr", "small", "llm", ["en"], "audio",
                        translation_llm_model="claude-opus-4-7")
    b = cache.cache_key("fp", "fr", "small", "llm", ["en"], "audio",
                        translation_llm_model="gpt-4o")
    c = cache.cache_key("fp", "fr", "small", "llm", ["en"], "audio",
                        translation_llm_model="qwen2.5:72b")
    assert len({a, b, c}) == 3


def test_cache_key_distinguishes_vision_llm_models_in_scene_mode():
    """In scene/cinematic, the bible depends on which LLM described the
    keyframes — a different vision model produces different descriptions and
    therefore a different translation."""
    a = cache.cache_key("fp", "fr", "small", "llm", ["en"], "scene",
                        scene_threshold=0.4,
                        translation_llm_model="claude-opus-4-7",
                        vision_llm_model="claude-opus-4-7")
    b = cache.cache_key("fp", "fr", "small", "llm", ["en"], "scene",
                        scene_threshold=0.4,
                        translation_llm_model="claude-opus-4-7",
                        vision_llm_model="qwen2.5-vl:72b")
    assert a != b


def test_cache_key_omits_llm_args_when_none():
    """Callers pass None for non-llm providers — those should produce the same
    key as omitting the kwarg entirely, so DeepL/NLLB jobs don't accidentally
    fragment the cache."""
    a = cache.cache_key("fp", "fr", "small", "deepl", ["en"], "audio")
    b = cache.cache_key("fp", "fr", "small", "deepl", ["en"], "audio",
                        translation_llm_model=None, vision_llm_model=None)
    assert a == b


def test_cache_load_missing_returns_none(tmp_path, monkeypatch):
    from app.config import settings as _settings
    monkeypatch.setattr(_settings._env, "cache_dir", tmp_path)
    assert cache.load("nonexistent-key") is None


def test_cache_load_corrupt_json_returns_none(tmp_path, monkeypatch):
    from app.config import settings as _settings
    monkeypatch.setattr(_settings._env, "cache_dir", tmp_path)
    (tmp_path / "broken.json").write_text("{not valid json")
    assert cache.load("broken") is None


def test_cache_store_and_load_roundtrip(tmp_path, monkeypatch):
    from app.config import settings as _settings
    monkeypatch.setattr(_settings._env, "cache_dir", tmp_path)
    payload = {"vtt": "WEBVTT\n\nfoo", "cue_count": 1}
    cache.store("k1", payload)
    assert cache.load("k1") == payload


# ── Content fingerprint: stable across mtime, path, metadata edits ───────────


def _make_media_file(path, size=4 * 1024 * 1024):
    """Build a fake media file with deterministic content. Default size is
    4 MB so the content fingerprint takes its two 64 KB samples (the
    sampling threshold is 2 * 1MB + 2 * 64KB ≈ 2.13 MB)."""
    # Predictable but non-trivial content so two non-identical files have
    # genuinely different bytes at the sample offsets.
    data = bytes(((i * 7 + 13) & 0xFF) for i in range(size))
    path.write_bytes(data)
    return path


def test_content_fingerprint_stable_across_mtime_change(tmp_path):
    """The headline use case: rsync, mkvpropedit, or any tool that bumps
    mtime without touching the data must NOT invalidate the cache. The
    quick fingerprint will miss; the content fingerprint catches the file."""
    import os
    p = _make_media_file(tmp_path / "movie.mkv")
    fp1 = cache.content_fingerprint(p)
    quick_before = cache.quick_fingerprint(p)
    # Bump mtime by a clear margin so int(mtime) ticks past the previous second.
    new_time = p.stat().st_mtime + 100
    os.utime(p, (new_time, new_time))
    fp2 = cache.content_fingerprint(p)
    assert fp1 == fp2, "content fingerprint must survive an mtime-only bump"
    # And the quick fingerprint MUST differ — otherwise this test isn't
    # actually exercising the difference between the two schemes.
    assert cache.quick_fingerprint(p) != quick_before


def test_content_fingerprint_stable_across_path_change(tmp_path):
    """Renaming or moving a file shouldn't invalidate the cache. The user
    reorganizes their library, the existing translations stay valid."""
    p1 = _make_media_file(tmp_path / "movie.mkv")
    fp1 = cache.content_fingerprint(p1)
    p2 = tmp_path / "subdir" / "renamed.mkv"
    p2.parent.mkdir()
    p1.rename(p2)
    fp2 = cache.content_fingerprint(p2)
    assert fp1 == fp2


def test_content_fingerprint_stable_across_header_only_edit(tmp_path):
    """mkvpropedit edits live in the EBML header (first ~100 KB-ish of an
    MKV). The content fingerprint samples at 1 MB and at the midpoint, so
    a header-only mutation must not change the fingerprint."""
    p = _make_media_file(tmp_path / "movie.mkv")
    fp_before = cache.content_fingerprint(p)
    # Mutate the first 100 KB — simulates an mkvpropedit edit of the EBML
    # header (real edits are smaller but this exercises the principle).
    raw = bytearray(p.read_bytes())
    for i in range(100 * 1024):
        raw[i] = (raw[i] + 1) & 0xFF
    p.write_bytes(bytes(raw))
    fp_after = cache.content_fingerprint(p)
    assert fp_before == fp_after, "header-only edit must not invalidate the cache"


def test_content_fingerprint_changes_when_data_section_changes(tmp_path):
    """Re-encoded audio, replaced file with different content, etc. — the
    fingerprint must invalidate so we don't serve stale subtitles."""
    p = _make_media_file(tmp_path / "movie.mkv")
    fp_before = cache.content_fingerprint(p)
    # Mutate bytes in the middle of the file (where the second sample reads)
    raw = bytearray(p.read_bytes())
    midpoint = len(raw) // 2
    for i in range(midpoint, midpoint + 1024):
        raw[i] = (raw[i] + 1) & 0xFF
    p.write_bytes(bytes(raw))
    fp_after = cache.content_fingerprint(p)
    assert fp_before != fp_after


def test_content_fingerprint_size_changes_invalidate(tmp_path):
    """Even if the sampled regions happen to match (vanishingly unlikely
    in practice), the file size is part of the hash so different lengths
    always differ."""
    p1 = tmp_path / "a.mkv"
    p2 = tmp_path / "b.mkv"
    p1.write_bytes(b"x" * 100)
    p2.write_bytes(b"x" * 200)
    assert cache.content_fingerprint(p1) != cache.content_fingerprint(p2)


def test_content_fingerprint_handles_small_files(tmp_path):
    """Files smaller than the sampling threshold (2 MB) are hashed in full.
    Cheap because they're small. The contract is: identical content →
    identical fingerprint."""
    p1 = tmp_path / "a.mkv"
    p2 = tmp_path / "b.mkv"  # different name, same content
    p1.write_bytes(b"hello")
    p2.write_bytes(b"hello")
    assert cache.content_fingerprint(p1) == cache.content_fingerprint(p2)


def test_quick_and_content_fingerprints_are_distinct(tmp_path):
    """Sanity: the two schemes don't accidentally produce the same hash
    for the same file (which would make the two-level lookup pointless)."""
    p = _make_media_file(tmp_path / "movie.mkv")
    assert cache.quick_fingerprint(p) != cache.content_fingerprint(p)


# ── Two-level lookup: relinks cache after mtime change ───────────────────────


def test_two_level_lookup_hits_content_fp_after_mtime_bump(tmp_path, monkeypatch):
    """The headline correctness check: a cache stored, mtime bumped, then
    looked up — must hit via the content fingerprint."""
    import os
    from app.config import settings as _settings
    monkeypatch.setattr(_settings._env, "cache_dir", tmp_path)

    media = _make_media_file(tmp_path / "movie.mkv")
    key_kwargs = dict(
        target_lang="fr", model="small", provider="nllb",
        source_priority=["en"], mode="audio",
    )
    payload = {"vtt": "WEBVTT", "cue_count": 1,
               "source_track": {"index": 1, "language": None, "title": None},
               "detected_source_language": "en", "mode": "audio"}

    cache.store_two_level(media, payload, **key_kwargs)

    # Bump mtime — quick fingerprint changes, content stays.
    new_time = media.stat().st_mtime + 60
    os.utime(media, (new_time, new_time))

    cached, quick_key, content_key = cache.lookup_two_level(media, **key_kwargs)
    assert cached == payload
    assert quick_key != content_key

    # And the relinking must have happened — a direct load by the new
    # quick_key now hits without paying for another content read.
    assert cache.load(quick_key) == payload


def test_two_level_lookup_invalidates_on_real_content_change(tmp_path, monkeypatch):
    """Real content change must produce a miss — both fingerprints change,
    no stale payload served."""
    import os
    from app.config import settings as _settings
    monkeypatch.setattr(_settings._env, "cache_dir", tmp_path)

    media = _make_media_file(tmp_path / "movie.mkv")
    key_kwargs = dict(
        target_lang="fr", model="small", provider="nllb",
        source_priority=["en"], mode="audio",
    )
    cache.store_two_level(media, {"vtt": "WEBVTT", "cue_count": 1,
                                  "source_track": {"index": 1, "language": None, "title": None},
                                  "detected_source_language": "en", "mode": "audio"},
                         **key_kwargs)

    # Re-encode simulation: mutate bytes near the midpoint.
    raw = bytearray(media.read_bytes())
    mid = len(raw) // 2
    for i in range(mid, mid + 1024):
        raw[i] = (raw[i] + 1) & 0xFF
    media.write_bytes(bytes(raw))
    # Force the quick fingerprint to differ too — write_bytes bumps mtime
    # but int(mtime) rounds to the second, so a sub-second mutation
    # (which test code does in milliseconds) wouldn't shift the quick fp.
    # In production this matters less: nobody re-encodes a 2-hour film in
    # under a second. Here we just make the test exercise the right path.
    new_time = media.stat().st_mtime + 100
    os.utime(media, (new_time, new_time))

    cached, _, _ = cache.lookup_two_level(media, **key_kwargs)
    assert cached is None


def test_two_level_lookup_skips_content_read_on_quick_hit(tmp_path, monkeypatch):
    """Performance contract: when the quick fingerprint hits, we must NOT
    pay for the ~10ms content fingerprint read. Verified by patching
    content_fingerprint to raise."""
    from app.config import settings as _settings
    monkeypatch.setattr(_settings._env, "cache_dir", tmp_path)

    media = _make_media_file(tmp_path / "movie.mkv")
    key_kwargs = dict(
        target_lang="fr", model="small", provider="nllb",
        source_priority=["en"], mode="audio",
    )
    cache.store_two_level(media, {"vtt": "x", "cue_count": 1,
                                  "source_track": {"index": 1, "language": None, "title": None},
                                  "detected_source_language": "en", "mode": "audio"},
                         **key_kwargs)

    def boom(_):
        raise AssertionError("content_fingerprint must not be called on quick hit")
    monkeypatch.setattr(cache, "content_fingerprint", boom)

    cached, _, _ = cache.lookup_two_level(media, **key_kwargs)
    assert cached is not None
