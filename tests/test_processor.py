"""Processor validation tests. Heavy externals (ffmpeg, Whisper, LLM) are
mocked — we only want to verify the validation gates and error mapping."""
import pytest

from app.config import settings
from app.processor import (
    BadRequest, MediaNotFound, ProcessRequest, SUPPORTED_MODES, process,
)


def _req(**overrides):
    base = dict(
        media_path="/nonexistent/file.mkv",
        target_lang="fr",
        source_lang_priority=["en", "*"],
        translation_provider="llm",
        mode="audio",
    )
    base.update(overrides)
    return ProcessRequest(**base)


def test_unknown_mode_raises_bad_request():
    with pytest.raises(BadRequest, match="unknown mode"):
        process(_req(mode="bogus"))


def test_supported_modes_cover_documented_set():
    assert "audio" in SUPPORTED_MODES
    assert "scene" in SUPPORTED_MODES
    assert "cinematic" in SUPPORTED_MODES


def test_scene_mode_requires_llm_provider(monkeypatch):
    monkeypatch.setattr(settings, "_overrides",
                        {**settings._overrides, "vision_llm_enabled": True})
    with pytest.raises(BadRequest, match="translation_provider='llm'"):
        process(_req(mode="scene", translation_provider="deepl"))


def test_scene_mode_requires_vision_enabled(monkeypatch):
    monkeypatch.setattr(settings, "_overrides",
                        {**settings._overrides, "vision_llm_enabled": False})
    with pytest.raises(BadRequest, match="Vision LLM"):
        process(_req(mode="scene"))


def test_cinematic_mode_requires_translation_vision(monkeypatch):
    monkeypatch.setattr(settings, "_overrides",
                        {**settings._overrides,
                         "vision_llm_enabled": True,
                         "translation_llm_supports_vision": False})
    with pytest.raises(BadRequest, match="cinematic"):
        process(_req(mode="cinematic"))


def test_audio_mode_with_missing_media_raises_media_not_found():
    with pytest.raises(MediaNotFound):
        process(_req(mode="audio", media_path="/no/such/file.mkv"))


def test_cancel_during_translate_does_not_leave_cache_entry(monkeypatch, tmp_path):
    """SAFETY GUARANTEE the user explicitly asked us to lock down: canceling a
    job mid-translation must not leave a cached partial result. A retry of the
    same item must always recompute from scratch.

    This test stubs the heavy externals (track probe, audio extraction, STT
    backend, translation provider) and triggers JobCanceled from inside the
    provider. It then asserts that no cache file was written under the
    settings.cache_dir."""
    from app import cache as cache_mod
    from app.config import settings as runtime_settings
    from app.jobs import JobCanceled
    from app.pipeline import audio, stt, tracks
    from app.pipeline.stt import Cue, TranscriptionResult
    from app import processor as processor_mod

    # Real on-disk media so media.exists() passes and content_fingerprint can
    # read bytes. Cache lives under tmp_path so we can assert it stays empty.
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"\x00" * 4096)  # enough for content_fingerprint's mid-file read
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(runtime_settings, "_overrides",
                        {**runtime_settings._overrides, "cache_dir": str(cache_dir)})
    # cache_dir is read-only via the settings property, so also patch the
    # module-level cache_path resolver to use tmp_path.
    monkeypatch.setattr(cache_mod.settings, "cache_dir", cache_dir, raising=False)

    # Stub probe/select to return a single track with English audio.
    fake_track = type("T", (), {"index": 0, "language": "en", "title": None,
                                  "codec": "aac", "channels": 2, "is_default": True})()
    monkeypatch.setattr(tracks, "probe", lambda *a, **kw: [fake_track])
    monkeypatch.setattr(tracks, "select", lambda *a, **kw: fake_track)

    # extract_audio is a context manager yielding a wav path; we don't read it,
    # the stubbed transcribe ignores it.
    from contextlib import contextmanager
    @contextmanager
    def fake_extract(*a, **kw):
        wav = tmp_path / "audio.wav"
        wav.write_bytes(b"")
        yield wav
    monkeypatch.setattr(audio, "extract_audio", fake_extract)

    # Whisper returns one fake cue so translation is reached.
    monkeypatch.setattr(stt, "transcribe", lambda *a, **kw: TranscriptionResult(
        detected_language="en",
        cues=[Cue(id=0, start=0.0, end=2.0, text="hello")],
    ))

    # Translation provider raises JobCanceled — this is what happens when the
    # user clicks cancel between batches. The pipeline must propagate this
    # without writing the cache.
    class CancelingProvider:
        def translate(self, cues, source_lang, target_lang, context=None,
                      *, progress=None, check_cancel=None):
            raise JobCanceled("user clicked cancel")

    # Override the dispatcher in processor's namespace so the call inside
    # process() resolves to our canceling stub instead of trying to load
    # the real NLLB provider (which would download HF weights).
    monkeypatch.setattr(processor_mod, "get_provider", lambda name: CancelingProvider())
    req = _req(mode="audio", media_path=str(media), translation_provider="nllb")

    with pytest.raises(JobCanceled):
        process(req)

    # The cache directory must contain no subtitle payloads. (The bible
    # cache is gated on multimodal modes which we're not in here.)
    leftover = list(cache_dir.glob("*.json"))
    assert leftover == [], f"cancel left cache entries behind: {leftover}"


def test_cancel_during_transcribe_does_not_leave_cache_entry(monkeypatch, tmp_path):
    """Earlier cancel point: the pipeline raises before we even reach the
    translation provider. Cache must still be empty."""
    from app import cache as cache_mod
    from app.config import settings as runtime_settings
    from app.jobs import JobCanceled
    from app.pipeline import audio, stt, tracks

    media = tmp_path / "movie.mkv"
    media.write_bytes(b"\x00" * 4096)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(runtime_settings, "_overrides",
                        {**runtime_settings._overrides, "cache_dir": str(cache_dir)})
    monkeypatch.setattr(cache_mod.settings, "cache_dir", cache_dir, raising=False)

    fake_track = type("T", (), {"index": 0, "language": "en", "title": None,
                                  "codec": "aac", "channels": 2, "is_default": True})()
    monkeypatch.setattr(tracks, "probe", lambda *a, **kw: [fake_track])
    monkeypatch.setattr(tracks, "select", lambda *a, **kw: fake_track)

    from contextlib import contextmanager
    @contextmanager
    def fake_extract(*a, **kw):
        wav = tmp_path / "audio.wav"
        wav.write_bytes(b"")
        yield wav
    monkeypatch.setattr(audio, "extract_audio", fake_extract)

    def canceling_transcribe(*a, **kw):
        raise JobCanceled("user clicked cancel during transcribe")
    monkeypatch.setattr(stt, "transcribe", canceling_transcribe)

    req = _req(mode="audio", media_path=str(media), translation_provider="nllb")
    with pytest.raises(JobCanceled):
        process(req)
    assert list(cache_dir.glob("*.json")) == []
