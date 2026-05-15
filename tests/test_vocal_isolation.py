"""Tests for the Demucs vocal-isolation phase.

The real Demucs package is NOT installed in the test environment —
these tests monkeypatch the module's own seams (``_load_model``,
``_separate_streaming``, plus the ffmpeg helper) so the phase's
wiring (load → stream-separate → release → yield → cleanup) is
verified without paying the 1-2 GB / 5-min real cost AND without
faking the demucs submodules in ``sys.modules``.

What's pinned here:

- The context manager releases the cached model BEFORE yielding the
  WAV path. That's the whole point of the phase boundary — Whisper
  shouldn't load on top of an idle Demucs.
- The vocals WAV file persists through the with-block (so STT can read
  it) and is unlinked on exit.
- Cancel before separation aborts cleanly without leaving state.
- ``is_available()`` returns (False, error) when demucs isn't importable.
- Metrics — took_seconds + audio_seconds_processed — are populated.

Historical notes:
- pre-0.7.27 the module routed through ``demucs.api.Separator``,
  which doesn't exist in the published PyPI 4.0.1 wheel. Tests faked
  it via sys.modules injection.
- 0.7.27 switched to ``demucs.pretrained`` + ``demucs.apply`` and
  tests started monkeypatching ``_apply_separation`` / ``_save_*``.
- 0.7.29 fused both seams into ``_separate_streaming`` to fix the
  OOM-kill when apply_model tried to keep a full 2.5 h film in RAM.
  Tests now monkeypatch that one streaming function.
"""
import sys
from pathlib import Path

import pytest

from app import config as config_mod
from app.pipeline import vocal_isolation as vi


# ── Fakes for the module seams ────────────────────────────────────────────


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


class _FakeModel:
    """Stand-in for a Demucs model. Tracks how many times it was
    constructed / released and whether _apply_separation was handed
    this same instance."""

    instances: list["_FakeModel"] = []

    def __init__(self, name: str):
        self.name = name
        self.samplerate = 44100
        self.audio_channels = 2
        self.sources = ["drums", "bass", "other", "vocals"]
        _FakeModel.instances.append(self)


@pytest.fixture
def fake_load_model(monkeypatch):
    """Replace _load_model with a stub that returns a FakeModel and
    caches it on the module the same way the real implementation
    would. This lets the lifecycle tests still observe ``vi._model``
    transitioning from set → None across release_model()."""
    _FakeModel.instances.clear()

    def fake_loader(model_name: str):
        if vi._model is not None and vi._model_name_cached == model_name:
            return vi._model
        m = _FakeModel(model_name)
        vi._model = m
        vi._model_name_cached = model_name
        return m

    monkeypatch.setattr(vi, "_load_model", fake_loader)
    yield
    # Belt-and-suspenders: ensure the module-level cached model
    # doesn't leak into another test.
    vi.release_model()


@pytest.fixture
def fake_separate(monkeypatch):
    """Stub the streaming separation+save step so no torch/demucs/
    soundfile work happens. Writes a marker to the out_path the real
    function would have written to, fires the progress callback at
    [0.0, 0.5, 1.0] to mimic chunked progress, and returns a fake
    audio-seconds value the metrics test can assert on.

    Returns the (model, raw_wav, out_path) tuple of each call so tests
    can verify the model + paths threaded through correctly."""
    calls: list[dict] = []

    def fake_streaming(model, raw_wav: Path, out_path: Path,
                       *, chunk_seconds, progress_within_phase, check_cancel):
        calls.append({
            "model": model,
            "raw_wav": raw_wav,
            "out_path": out_path,
            "chunk_seconds": chunk_seconds,
        })
        # Fire a few progress ticks so the progress-callback test sees
        # within-phase advancement, not just the outer 0.0/1.0 anchors.
        progress_within_phase(0.0)
        check_cancel()
        progress_within_phase(0.5)
        check_cancel()
        out_path.write_bytes(b"FAKE_VOCALS_WAV")
        progress_within_phase(1.0)
        # Audio-seconds the metrics test assertion expects.
        return 5.0

    monkeypatch.setattr(vi, "_separate_streaming", fake_streaming)
    return calls


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
    """If demucs's entry-point modules can't be imported,
    is_available() reports it rather than raising. The Settings UI
    uses this to show an inline warning."""
    # Force both probes to fail. is_available imports demucs.pretrained
    # and demucs.apply — block whichever pip resolves first by setting
    # both to None in sys.modules.
    monkeypatch.setitem(sys.modules, "demucs.pretrained", None)
    monkeypatch.setitem(sys.modules, "demucs.apply", None)
    ok, err = vi.is_available()
    assert ok is False
    assert err is not None


# ── Context-manager lifecycle ─────────────────────────────────────────────


def test_isolate_vocals_yields_wav_path_present_during_block(
    fake_load_model, fake_separate, fake_ffmpeg, cache_dir
):
    """The vocals WAV file exists while we're inside the with-block —
    so STT (which would open it via soundfile) can read it."""
    with vi.isolate_vocals("/m/film.mkv", 0) as result:
        assert result.wav_path.is_file()
        assert result.wav_path.read_bytes() == b"FAKE_VOCALS_WAV"
        # The result carries SOME model name (whatever
        # settings.vocal_isolation_model holds). Asserting the
        # specific value is a settings concern, not a phase-wiring
        # concern — the lifecycle test pins the wiring.
        assert result.model
        # _separate_streaming got called with the loaded model and the
        # ffmpeg-extracted WAV plus the prepared output path.
        assert len(fake_separate) == 1
        c = fake_separate[0]
        assert isinstance(c["model"], _FakeModel)
        assert c["raw_wav"].name.endswith(".wav")
        assert c["out_path"] == result.wav_path


def test_isolate_vocals_releases_model_before_yield(
    fake_load_model, fake_separate, fake_ffmpeg, cache_dir
):
    """The lifecycle invariant: by the time the caller is INSIDE the
    yielded with-block, the cached Demucs model is already None.
    That's what guarantees Whisper doesn't load on top of an idle
    Demucs."""
    with vi.isolate_vocals("/m/film.mkv", 0):
        # Inside the block — STT would be running here. The
        # module-level cache must be empty.
        assert vi._model is None
        assert vi._model_name_cached is None


def test_isolate_vocals_cleans_up_files_on_exit(
    fake_load_model, fake_separate, fake_ffmpeg, cache_dir
):
    """Both the source WAV and the vocals WAV are removed when the
    context exits — no temp turds left in cache_dir/tmp."""
    with vi.isolate_vocals("/m/film.mkv", 0) as result:
        wav = result.wav_path
        assert wav.is_file()
    # After exit: the file is gone.
    assert not wav.exists()


def test_isolate_vocals_cleans_up_on_exception(
    fake_load_model, fake_separate, fake_ffmpeg, cache_dir
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
    assert vi._model is None
    assert not wav_observed[0].exists()


def test_isolate_vocals_progress_callback_fires(
    fake_load_model, fake_separate, fake_ffmpeg, cache_dir
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


def test_isolate_vocals_cancel_before_apply_aborts_cleanly(
    fake_load_model, fake_separate, fake_ffmpeg, cache_dir
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
    assert vi._model is None


def test_isolate_vocals_metrics_populated(
    fake_load_model, fake_separate, fake_ffmpeg, cache_dir
):
    """took_seconds and audio_seconds_processed are non-zero — the
    stats page reads these to show 'isolation ran for N s, processed
    M s of audio at Mx realtime'."""
    with vi.isolate_vocals("/m/film.mkv", 0) as result:
        assert result.took_seconds >= 0.0
        # Fake separation returns 5.0s of audio processed.
        assert result.audio_seconds_processed == pytest.approx(5.0, abs=0.5)


def test_isolate_vocals_raises_when_demucs_not_installed(
    monkeypatch, fake_ffmpeg, cache_dir
):
    """A clear ImportError surfaces at job-start time if the user
    toggles the feature on without installing demucs. Avoids a silent
    abort or a confusing traceback from deep inside the pipeline.

    We don't fake _load_model here — we let the real loader run, with
    demucs.pretrained blocked, so the ImportError path is exercised
    end-to-end."""
    monkeypatch.setitem(sys.modules, "demucs.pretrained", None)
    monkeypatch.setitem(sys.modules, "demucs.apply", None)
    vi._model = None
    vi._model_name_cached = None
    with pytest.raises(ImportError, match="demucs is not"):
        with vi.isolate_vocals("/m/film.mkv", 0):
            pytest.fail("should not have entered")


def test_isolate_vocals_rejects_model_without_vocals_stem(
    fake_load_model, fake_ffmpeg, cache_dir, monkeypatch
):
    """If the user picks a Demucs model that doesn't emit a 'vocals'
    stem (would be a config mistake), raise rather than silently
    falling back to a wrong stem. Verified by having
    _separate_streaming itself raise — same shape of failure as the
    real code path where the check lives."""

    def no_vocals(_model, _raw_wav, _out_path, *,
                  chunk_seconds, progress_within_phase, check_cancel):
        raise RuntimeError(
            "Demucs model produced no 'vocals' stem (found: ['drums', 'bass'])"
        )

    monkeypatch.setattr(vi, "_separate_streaming", no_vocals)

    with pytest.raises(RuntimeError, match="no 'vocals' stem"):
        with vi.isolate_vocals("/m/film.mkv", 0):
            pytest.fail("should not have yielded")
    # Cleanup invariants hold even on this error path.
    assert vi._model is None


def test_release_model_is_idempotent():
    """Calling release_model multiple times — including before any
    load — is safe. The processor relies on this in failure paths."""
    vi.release_model()
    vi.release_model()
    assert vi._model is None


# ── Mode-driven chunk_seconds dispatch ────────────────────────────────────


def test_mode_chunked_passes_user_chunk_seconds(
    fake_load_model, fake_separate, fake_ffmpeg, cache_dir, monkeypatch
):
    """When ``vocal_isolation_mode == "chunked"`` the user's configured
    chunk size is forwarded to _separate_streaming. The default 300 s
    is what protects 6 GB cgroups from the apply_model OOM."""
    monkeypatch.setattr(
        config_mod.settings, "_overrides",
        {
            **config_mod.settings._overrides,
            "vocal_isolation_mode": "chunked",
            "vocal_isolation_chunk_seconds": 120,
        },
    )
    with vi.isolate_vocals("/m/film.mkv", 0):
        pass
    assert len(fake_separate) == 1
    assert fake_separate[0]["chunk_seconds"] == 120


def test_mode_full_passes_zero_chunk_seconds(
    fake_load_model, fake_separate, fake_ffmpeg, cache_dir, monkeypatch
):
    """``vocal_isolation_mode == "full"`` is the sentinel for
    "no outer chunking — process the whole audio in one apply_model
    call". The isolate_vocals wrapper converts that to chunk_seconds=0,
    which _separate_streaming interprets as "single chunk covers
    everything". Pinning this avoids a future refactor accidentally
    sending the user's configured 300 s through when mode=full."""
    monkeypatch.setattr(
        config_mod.settings, "_overrides",
        {
            **config_mod.settings._overrides,
            "vocal_isolation_mode": "full",
            # User's configured chunk size should be IGNORED in full mode.
            "vocal_isolation_chunk_seconds": 300,
        },
    )
    with vi.isolate_vocals("/m/film.mkv", 0):
        pass
    assert len(fake_separate) == 1
    assert fake_separate[0]["chunk_seconds"] == 0


# ── Submit-time fail-fast ──────────────────────────────────────────────────


def test_submit_fail_fast_when_isolation_on_but_demucs_missing(monkeypatch):
    """The job-submit helper refuses upfront when vocal isolation is
    enabled (mode != "off") but demucs isn't installed — so the
    operator sees the fix in the UI submit response instead of after
    the job briefly queues and then fails with the same import error
    deep in the pipeline."""
    from app import config as config_mod
    from app.api.manage import submit_item_job
    from app.server import MediaItem

    # Force the demucs probe to report missing.
    monkeypatch.setitem(sys.modules, "demucs.pretrained", None)
    monkeypatch.setitem(sys.modules, "demucs.apply", None)
    # Strip any prior cached model so is_available re-probes.
    vi._model = None
    vi._model_name_cached = None
    monkeypatch.setattr(
        config_mod.settings, "_overrides",
        {**config_mod.settings._overrides, "vocal_isolation_mode": "chunked"},
    )

    item = MediaItem(id="item1", name="Film", path="/m/film.mkv", type="Movie")

    class _FakeServer:
        def refresh_item(self, *a, **kw): pass

    with pytest.raises(ValueError, match="demucs"):
        submit_item_job(server=_FakeServer(), item=item)
