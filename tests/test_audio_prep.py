"""Tests for the 0.7.33 audio-prep upgrades:

- ``probe_channel_layout`` correctly identifies 5.1+ sources via
  ffprobe stdout parsing.
- ``_build_filter_chain`` chooses center-channel extraction (FC) for
  5.1+ and stereo-downmix-with-encoder-flag for ≤ 2.0 sources.
- The full ffmpeg invocation in ``extract_audio`` includes loudnorm
  on the audio filter list AND maps the right track.

Real ffmpeg/ffprobe never run — we stub subprocess.run to capture
the arg list.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline import audio as audio_mod


# ── probe_channel_layout ────────────────────────────────────────────────────


def _stub_ffprobe_json(payload: dict):
    """Build a CompletedProcess-like return for the ffprobe call."""
    cp = MagicMock()
    cp.stdout = json.dumps(payload)
    cp.returncode = 0
    return cp


def test_probe_channel_layout_detects_51_layout(monkeypatch):
    payload = {"streams": [{"channels": 6, "channel_layout": "5.1(side)"}]}
    monkeypatch.setattr(audio_mod.subprocess, "run",
                        lambda *a, **kw: _stub_ffprobe_json(payload))
    info = audio_mod.probe_channel_layout("/m/f.mkv", 1)
    assert info.channels == 6
    assert info.layout == "5.1(side)"
    assert info.has_center is True


def test_probe_channel_layout_detects_71(monkeypatch):
    payload = {"streams": [{"channels": 8, "channel_layout": "7.1"}]}
    monkeypatch.setattr(audio_mod.subprocess, "run",
                        lambda *a, **kw: _stub_ffprobe_json(payload))
    info = audio_mod.probe_channel_layout("/m/f.mkv", 1)
    assert info.has_center is True


def test_probe_channel_layout_stereo_has_no_center(monkeypatch):
    payload = {"streams": [{"channels": 2, "channel_layout": "stereo"}]}
    monkeypatch.setattr(audio_mod.subprocess, "run",
                        lambda *a, **kw: _stub_ffprobe_json(payload))
    info = audio_mod.probe_channel_layout("/m/f.mkv", 1)
    assert info.channels == 2
    assert info.has_center is False


def test_probe_channel_layout_mono_has_no_center(monkeypatch):
    payload = {"streams": [{"channels": 1, "channel_layout": "mono"}]}
    monkeypatch.setattr(audio_mod.subprocess, "run",
                        lambda *a, **kw: _stub_ffprobe_json(payload))
    info = audio_mod.probe_channel_layout("/m/f.mkv", 1)
    assert info.has_center is False


def test_probe_channel_layout_treats_6_channels_without_layout_as_51(monkeypatch):
    """Some weird remuxes report 6 channels with no layout tag.
    Conservative gate: ≥6 channels → almost certainly 5.1 → has_center."""
    payload = {"streams": [{"channels": 6}]}
    monkeypatch.setattr(audio_mod.subprocess, "run",
                        lambda *a, **kw: _stub_ffprobe_json(payload))
    info = audio_mod.probe_channel_layout("/m/f.mkv", 1)
    assert info.has_center is True


def test_probe_channel_layout_returns_safe_fallback_on_ffprobe_error(monkeypatch):
    """A broken ffprobe must NOT crash the pipeline — fall back to
    ``has_center=False`` so the caller picks the standard downmix path."""
    import subprocess
    def boom(*a, **kw):
        raise subprocess.CalledProcessError(1, "ffprobe")
    monkeypatch.setattr(audio_mod.subprocess, "run", boom)
    info = audio_mod.probe_channel_layout("/m/f.mkv", 1)
    assert info.channels == 0
    assert info.layout is None
    assert info.has_center is False


def test_probe_channel_layout_returns_safe_fallback_on_bad_json(monkeypatch):
    cp = MagicMock()
    cp.stdout = "not json {"
    cp.returncode = 0
    monkeypatch.setattr(audio_mod.subprocess, "run", lambda *a, **kw: cp)
    info = audio_mod.probe_channel_layout("/m/f.mkv", 1)
    assert info.has_center is False


# ── _build_filter_chain ─────────────────────────────────────────────────────


def test_filter_chain_for_51_uses_fc_pan_and_loudnorm():
    info = audio_mod.ChannelInfo(channels=6, layout="5.1", has_center=True)
    filters, extra_flags = audio_mod._build_filter_chain(info)
    assert filters == ["pan=mono|c0=FC", "loudnorm=I=-23:LRA=11:TP=-1.5"]
    # When the filter outputs mono already (pan), we DON'T add -ac 1 too.
    assert "-ac" not in extra_flags


def test_filter_chain_for_stereo_uses_loudnorm_and_ac1():
    info = audio_mod.ChannelInfo(channels=2, layout="stereo", has_center=False)
    filters, extra_flags = audio_mod._build_filter_chain(info)
    # No pan filter, just loudnorm. Mono downmix happens at encoder level
    # via -ac 1.
    assert filters == ["loudnorm=I=-23:LRA=11:TP=-1.5"]
    assert "-ac" in extra_flags
    assert "1" in extra_flags


def test_filter_chain_for_mono_uses_loudnorm_and_ac1():
    """Mono input → no pan needed but still -ac 1 (no-op, but harmless)
    and loudnorm to bring the track into Whisper's training range."""
    info = audio_mod.ChannelInfo(channels=1, layout="mono", has_center=False)
    filters, extra_flags = audio_mod._build_filter_chain(info)
    assert "loudnorm=I=-23:LRA=11:TP=-1.5" in filters
    assert "-ac" in extra_flags


# ── extract_audio integration ───────────────────────────────────────────────


def test_extract_audio_emits_fc_pan_for_51_source(tmp_path, monkeypatch):
    """End-to-end: when ffprobe reports 6 channels with FC, the ffmpeg
    invocation MUST include the center-channel pan filter on -af. This
    is the headline guarantee of the whole 0.7.33 audio-prep change."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    from app.config import settings as runtime_settings
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": cache_dir},
    )

    # Stub ffprobe to claim 5.1, ffmpeg to no-op.
    captured: dict = {}

    def fake_run(args, **kwargs):
        # Differentiate ffprobe (-show_entries) vs ffmpeg (-i + -af).
        if "ffprobe" in args[0]:
            cp = MagicMock()
            cp.stdout = json.dumps({"streams": [{"channels": 6, "channel_layout": "5.1"}]})
            cp.returncode = 0
            return cp
        captured["ffmpeg_args"] = list(args)
        cp = MagicMock()
        cp.returncode = 0
        return cp

    monkeypatch.setattr(audio_mod.subprocess, "run", fake_run)

    with audio_mod.extract_audio("/m/f.mkv", 1) as wav:
        # ffmpeg ran (we captured its args). Verify the -af string.
        args = captured["ffmpeg_args"]
        # -af should be present and include both the pan and loudnorm filters.
        af_index = args.index("-af")
        filter_string = args[af_index + 1]
        assert "pan=mono|c0=FC" in filter_string
        assert "loudnorm=I=-23" in filter_string
        # No -ac 1 (pan handles the mono output).
        assert "-ac" not in args
        # Still 16 kHz mono PCM_S16LE for Whisper compatibility.
        assert "-ar" in args and "16000" in args
        assert "-c:a" in args and "pcm_s16le" in args


def test_extract_audio_falls_back_when_optimised_chain_fails(tmp_path, monkeypatch):
    """Safety net: ffprobe says 5.1 → we try pan=mono|c0=FC. If ffmpeg
    rejects it (non-standard layout where FC doesn't exist despite the
    channel count), we fall back to standard downmix instead of failing
    the job. The user still gets a valid 16 kHz mono WAV, just without
    the center-channel optimisation."""
    import subprocess as sub_mod
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    from app.config import settings as runtime_settings
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": cache_dir},
    )

    captured_calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        if "ffprobe" in args[0]:
            cp = MagicMock()
            cp.stdout = json.dumps({"streams": [{"channels": 6, "channel_layout": "5.1"}]})
            cp.returncode = 0
            return cp
        captured_calls.append(list(args))
        # First ffmpeg call (the FC-pan optimised path) FAILS.
        # Second call (the fallback) succeeds.
        af_arg = args[args.index("-af") + 1] if "-af" in args else ""
        if "pan=mono|c0=FC" in af_arg:
            raise sub_mod.CalledProcessError(1, args, stderr=b"pan: FC not found")
        cp = MagicMock()
        cp.returncode = 0
        return cp

    monkeypatch.setattr(audio_mod.subprocess, "run", fake_run)

    # The whole thing must succeed despite the first ffmpeg failing.
    with audio_mod.extract_audio("/m/f.mkv", 1) as wav:
        pass

    # Exactly two ffmpeg calls were made: optimised (failed) + fallback (succeeded).
    assert len(captured_calls) == 2
    first_af = captured_calls[0][captured_calls[0].index("-af") + 1]
    second_af = captured_calls[1][captured_calls[1].index("-af") + 1]
    assert "pan=mono|c0=FC" in first_af   # optimised path tried first
    assert "pan=" not in second_af        # fallback drops the pan filter
    assert "loudnorm=I=-23" in second_af  # loudnorm still applied on fallback
    # -ac 1 present on the fallback for mono output (no pan to handle it).
    assert "-ac" in captured_calls[1]


def test_extract_audio_propagates_failure_on_non_center_path(tmp_path, monkeypatch):
    """When ffprobe reports a non-5.1 source AND ffmpeg fails, there's
    no safer fallback to try — bubble the error up."""
    import subprocess as sub_mod
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    from app.config import settings as runtime_settings
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": cache_dir},
    )

    def fake_run(args, **kwargs):
        if "ffprobe" in args[0]:
            cp = MagicMock()
            cp.stdout = json.dumps({"streams": [{"channels": 2, "channel_layout": "stereo"}]})
            cp.returncode = 0
            return cp
        raise sub_mod.CalledProcessError(1, args, stderr=b"some other error")

    monkeypatch.setattr(audio_mod.subprocess, "run", fake_run)

    with pytest.raises(sub_mod.CalledProcessError):
        with audio_mod.extract_audio("/m/f.mkv", 1):
            pass


def test_extract_audio_uses_standard_downmix_for_stereo(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    from app.config import settings as runtime_settings
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": cache_dir},
    )

    captured: dict = {}

    def fake_run(args, **kwargs):
        if "ffprobe" in args[0]:
            cp = MagicMock()
            cp.stdout = json.dumps({"streams": [{"channels": 2, "channel_layout": "stereo"}]})
            return cp
        captured["ffmpeg_args"] = list(args)
        cp = MagicMock()
        return cp

    monkeypatch.setattr(audio_mod.subprocess, "run", fake_run)

    with audio_mod.extract_audio("/m/f.mkv", 1) as wav:
        args = captured["ffmpeg_args"]
        af_index = args.index("-af")
        filter_string = args[af_index + 1]
        # Stereo → loudnorm only, no pan.
        assert "pan=" not in filter_string
        assert "loudnorm=I=-23" in filter_string
        # -ac 1 IS present for stereo input (mono downmix at encoder).
        assert "-ac" in args
        # Order: -ac 1 comes BEFORE -ar 16000 in the arg list.
        assert args[args.index("-ac") + 1] == "1"
