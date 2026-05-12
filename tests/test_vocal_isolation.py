"""Tests for the Demucs vocal-isolation phase.

The real Demucs package is NOT installed in the test environment —
these tests fake the ``demucs.api`` import at the module level so the
phase's wiring (load → run → release → yield → cleanup) is verified
without paying the 1-2 GB / 5-min real cost.

What's pinned here:

- The context manager releases the cached separator BEFORE yielding
  the WAV path. That's the whole point of the phase boundary — Whisper
  shouldn't load on top of an idle Demucs.
- The vocals WAV file persists through the with-block (so STT can read
  it) and is unlinked on exit.
- Cancel before separation aborts cleanly without leaving state.
- ``is_available()`` returns (False, error) when demucs isn't importable.
- Metrics — took_seconds + audio_seconds_processed — are populated.
"""
import sys
import types
from pathlib import Path

import pytest

from app import config as config_mod
from app.pipeline import vocal_isolation as vi


# ── Fake demucs.api injection ─────────────────────────────────────────────


class _FakeTensor:
    """Minimal stand-in for a torch tensor — only the attributes the
    pipeline code touches. Enough surface for shape inspection and a
    no-op mean/squeeze/clamp/numpy chain in _save_vocals_as_whisper_wav."""

    def __init__(self, channels=2, samples=44100 * 5):
        self.shape = (channels, samples)
        self._channels = channels
        self._samples = samples

    def dim(self):
        return 2

    def mean(self, dim, keepdim=True):
        return _FakeTensor(channels=1, samples=self._samples)

    def squeeze(self, _dim):
        return self

    def clamp(self, _lo, _hi):
        return self

    def numpy(self):
        import numpy as np
        return np.zeros(self._samples, dtype="float32")


class _FakeSeparator:
    """Mimics demucs.api.Separator. Records construction args + tracks
    whether ``separate_audio_file`` was called and on what path."""

    instances: list["_FakeSeparator"] = []

    def __init__(self, *, model, device, progress):
        self.model_name = model
        self.device = device
        self.progress = progress
        self.samplerate = 44100
        self.separate_called_with = None
        _FakeSeparator.instances.append(self)

    def separate_audio_file(self, path):
        self.separate_called_with = path
        origin = _FakeTensor(channels=2, samples=44100 * 5)
        # Emit a stems dict — Demucs htdemucs shape, vocals key matters.
        stems = {
            "drums": _FakeTensor(channels=2, samples=44100 * 5),
            "bass":  _FakeTensor(channels=2, samples=44100 * 5),
            "other": _FakeTensor(channels=2, samples=44100 * 5),
            "vocals": _FakeTensor(channels=2, samples=44100 * 5),
        }
        return origin, stems


@pytest.fixture
def fake_demucs(monkeypatch):
    """Inject a fake ``demucs.api`` into sys.modules for the duration
    of the test. The vocal_isolation module imports lazily, so this
    must be set up BEFORE the context manager enters."""
    _FakeSeparator.instances.clear()
    fake_api = types.ModuleType("demucs.api")
    fake_api.Separator = _FakeSeparator
    fake_pkg = types.ModuleType("demucs")
    fake_pkg.api = fake_api
    monkeypatch.setitem(sys.modules, "demucs", fake_pkg)
    monkeypatch.setitem(sys.modules, "demucs.api", fake_api)
    yield
    # Belt-and-suspenders: ensure the module-level cached separator
    # doesn't leak into another test.
    vi.release_model()


@pytest.fixture
def fake_ffmpeg(monkeypatch, tmp_path):
    """Stub the ffmpeg extract step so no real ffmpeg is needed. Writes
    a tiny placeholder file at the path the real ffmpeg would have
    written to, then returns it."""
    tmp_dir = tmp_path / "cache" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    def fake_extract(_media_path, _track_index):
        out = tmp_dir / "fake_demucs_in.wav"
        out.write_bytes(b"\x00" * 16)
        return out

    monkeypatch.setattr(vi, "_ffmpeg_extract_for_demucs", fake_extract)


@pytest.fixture
def fake_save(monkeypatch):
    """Stub the torchaudio save step — the fake tensor doesn't survive
    a real resample/write. Replaces _save_vocals_as_whisper_wav with
    a marker write so we can verify the function was reached AND that
    the output path is the one passed to it."""
    written: list[Path] = []

    def fake_writer(_vocals, _sr, out_path):
        out_path.write_bytes(b"FAKE_VOCALS_WAV")
        written.append(out_path)

    monkeypatch.setattr(vi, "_save_vocals_as_whisper_wav", fake_writer)
    return written


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point settings.cache_dir at tmp_path/cache so the module's
    Path(settings.cache_dir) / 'tmp' lookups resolve under tmp_path."""
    cdir = tmp_path / "cache"
    cdir.mkdir(exist_ok=True)
    if "cache_dir" in config_mod.settings.__dict__:
        monkeypatch.delattr(config_mod.settings, "cache_dir", raising=False)
    monkeypatch.setattr(
        config_mod.settings, "_overrides",
        {**config_mod.settings._overrides, "cache_dir": str(cdir)},
    )
    return cdir


# ── is_available() ────────────────────────────────────────────────────────


def test_is_available_returns_false_when_demucs_missing(monkeypatch):
    """If demucs.api can't be imported, is_available() reports it
    rather than raising. The Settings UI uses this to show an inline
    warning."""
    # Force the import to fail.
    monkeypatch.setitem(sys.modules, "demucs.api", None)
    ok, err = vi.is_available()
    assert ok is False
    assert err is not None


def test_is_available_returns_true_when_demucs_present(fake_demucs):
    """With a fake demucs.api injected, the probe succeeds."""
    ok, err = vi.is_available()
    assert ok is True
    assert err is None


# ── Context-manager lifecycle ─────────────────────────────────────────────


def test_isolate_vocals_yields_wav_path_present_during_block(
    fake_demucs, fake_ffmpeg, fake_save, cache_dir
):
    """The vocals WAV file exists while we're inside the with-block —
    so STT (which would open it via soundfile) can read it."""
    with vi.isolate_vocals("/m/film.mkv", 0) as result:
        assert result.wav_path.is_file()
        assert result.wav_path.read_bytes() == b"FAKE_VOCALS_WAV"
        assert result.model == "htdemucs"
        # Demucs got called on the fake extracted WAV.
        assert _FakeSeparator.instances[-1].separate_called_with is not None


def test_isolate_vocals_releases_model_before_yield(
    fake_demucs, fake_ffmpeg, fake_save, cache_dir
):
    """The lifecycle invariant: by the time the caller is INSIDE the
    yielded with-block, the cached Demucs separator is already None.
    That's what guarantees Whisper doesn't load on top of an idle
    Demucs."""
    with vi.isolate_vocals("/m/film.mkv", 0):
        # Inside the block — STT would be running here. The
        # module-level cache must be empty.
        assert vi._separator is None
        assert vi._model_name_cached is None


def test_isolate_vocals_cleans_up_files_on_exit(
    fake_demucs, fake_ffmpeg, fake_save, cache_dir
):
    """Both the source WAV and the vocals WAV are removed when the
    context exits — no temp turds left in cache_dir/tmp."""
    with vi.isolate_vocals("/m/film.mkv", 0) as result:
        wav = result.wav_path
        assert wav.is_file()
    # After exit: the file is gone.
    assert not wav.exists()


def test_isolate_vocals_cleans_up_on_exception(
    fake_demucs, fake_ffmpeg, fake_save, cache_dir
):
    """If the caller raises inside the with-block (e.g. STT crashed),
    the finally clause still releases the model and unlinks the
    files."""
    wav_observed: list[Path] = []
    with pytest.raises(RuntimeError, match="stt fail"):
        with vi.isolate_vocals("/m/film.mkv", 0) as result:
            wav_observed.append(result.wav_path)
            assert result.wav_path.is_file()
            raise RuntimeError("stt fail")
    assert vi._separator is None
    assert not wav_observed[0].exists()


def test_isolate_vocals_progress_callback_fires(
    fake_demucs, fake_ffmpeg, fake_save, cache_dir
):
    """The progress callback receives fractional updates [0.0, 1.0] —
    used by the outer processor to drive the dashboard progress bar."""
    seen = []
    with vi.isolate_vocals(
        "/m/film.mkv", 0, progress=lambda f: seen.append(f),
    ):
        pass
    assert seen[0] == 0.0
    assert seen[-1] == 1.0
    assert all(0.0 <= f <= 1.0 for f in seen)


def test_isolate_vocals_cancel_before_separate_aborts_cleanly(
    fake_demucs, fake_ffmpeg, fake_save, cache_dir
):
    """If the user cancels BEFORE Demucs runs, check_cancel raises and
    the finally clause releases / cleans up. The vocals WAV path
    placeholder gets unlinked too."""
    class _Cancel(Exception): ...
    calls = [0]
    def cancel():
        calls[0] += 1
        if calls[0] >= 2:    # raise on the second check_cancel call
            raise _Cancel("user canceled")
    with pytest.raises(_Cancel):
        with vi.isolate_vocals(
            "/m/film.mkv", 0, check_cancel=cancel,
        ):
            pytest.fail("should not have yielded")
    assert vi._separator is None


def test_isolate_vocals_metrics_populated(
    fake_demucs, fake_ffmpeg, fake_save, cache_dir
):
    """took_seconds and audio_seconds_processed are non-zero — the
    stats page reads these to show 'isolation ran for N s, processed
    M s of audio at Mx realtime'."""
    with vi.isolate_vocals("/m/film.mkv", 0) as result:
        assert result.took_seconds >= 0.0
        # Fake stems are 5s long at 44.1k → audio_seconds_processed=5.0
        assert result.audio_seconds_processed == pytest.approx(5.0, abs=0.5)


def test_isolate_vocals_raises_when_demucs_not_installed(
    monkeypatch, fake_ffmpeg, fake_save, cache_dir
):
    """A clear ImportError surfaces at job-start time if the user
    toggles the feature on without installing demucs. Avoids a silent
    abort or a confusing traceback from deep inside the pipeline."""
    monkeypatch.setitem(sys.modules, "demucs.api", None)
    vi._separator = None
    vi._model_name_cached = None
    with pytest.raises(ImportError, match="demucs is not installed"):
        with vi.isolate_vocals("/m/film.mkv", 0):
            pytest.fail("should not have entered")


def test_isolate_vocals_rejects_model_without_vocals_stem(
    fake_demucs, fake_ffmpeg, fake_save, cache_dir, monkeypatch
):
    """If the user picks a Demucs model that doesn't emit a 'vocals'
    stem (would be a config mistake), raise rather than silently
    falling back to a wrong stem."""

    class _NoVocalsSeparator(_FakeSeparator):
        def separate_audio_file(self, path):
            origin = _FakeTensor()
            return origin, {"drums": _FakeTensor(), "bass": _FakeTensor()}

    fake_api = sys.modules["demucs.api"]
    monkeypatch.setattr(fake_api, "Separator", _NoVocalsSeparator)
    vi._separator = None  # force re-load

    with pytest.raises(RuntimeError, match="no 'vocals' stem"):
        with vi.isolate_vocals("/m/film.mkv", 0):
            pytest.fail("should not have yielded")
    # Cleanup invariants hold even on this error path.
    assert vi._separator is None


def test_release_model_is_idempotent(fake_demucs):
    """Calling release_model multiple times — including before any
    load — is safe. The processor relies on this in failure paths."""
    vi.release_model()
    vi.release_model()
    assert vi._separator is None


# ── Submit-time fail-fast ──────────────────────────────────────────────────


def test_submit_fail_fast_when_isolation_on_but_demucs_missing(monkeypatch):
    """The job-submit helper refuses upfront when vocal_isolation_enabled
    is True but demucs isn't installed — so the operator sees the fix
    in the UI submit response instead of after the job briefly queues
    and then fails with the same import error deep in the pipeline."""
    from app import config as config_mod
    from app.api.manage import submit_item_job
    from app.server import MediaItem

    # Force the demucs probe to report missing.
    monkeypatch.setitem(sys.modules, "demucs.api", None)
    # Strip any prior cached separator so is_available re-probes.
    vi._separator = None
    vi._model_name_cached = None
    monkeypatch.setattr(
        config_mod.settings, "_overrides",
        {**config_mod.settings._overrides, "vocal_isolation_enabled": True},
    )

    item = MediaItem(id="item1", name="Film", path="/m/film.mkv", type="Movie")

    class _FakeServer:
        def refresh_item(self, *a, **kw): pass

    with pytest.raises(ValueError, match="demucs"):
        submit_item_job(server=_FakeServer(), item=item)
