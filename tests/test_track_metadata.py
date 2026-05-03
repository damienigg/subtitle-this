"""Unit tests for the language tag write-back. Heavy externals (mkvpropedit,
ffmpeg, ffprobe) are mocked — we test the dispatch logic + error paths."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.pipeline import track_metadata


def _fake_ffprobe(audio_indices: list[int]):
    """Build a CompletedProcess that mimics ffprobe -select_streams a output."""
    return subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"streams": [{"index": i} for i in audio_indices]}),
        stderr="",
    )


def _ok_proc():
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _failed_proc(stderr: str = "boom"):
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def test_unknown_language_raises_without_subprocessing(tmp_path):
    """When we have no ISO 639-2 mapping for the detected code, we fail
    fast before shelling out to anything."""
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"\x00")
    with patch("subprocess.run") as run:
        with pytest.raises(track_metadata.MetadataWriteError, match="no ISO 639-2 mapping"):
            track_metadata.write_audio_language(f, 1, "xyz-not-a-real-lang")
        assert run.call_count == 0


def test_mkv_dispatches_to_mkvpropedit_with_audio_track_position(tmp_path):
    """For Matroska files, we should call mkvpropedit with the 1-based audio
    track position (NOT the absolute stream index)."""
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"\x00")
    # File has video at index 0, then 3 audio streams at 1, 2, 3.
    # We want to tag the SECOND audio stream (absolute index 2 → position 2).
    calls = []

    def run_stub(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "ffprobe":
            return _fake_ffprobe([1, 2, 3])
        if cmd[0] == "mkvpropedit":
            return _ok_proc()
        raise AssertionError(f"unexpected subprocess call: {cmd[0]}")

    with patch("subprocess.run", side_effect=run_stub):
        track_metadata.write_audio_language(f, 2, "fr")

    mkv_call = next(c for c in calls if c[0] == "mkvpropedit")
    assert "track:a2" in mkv_call
    assert "language=fra" in mkv_call


def test_non_matroska_container_refuses_safely(tmp_path):
    """We deliberately don't ffmpeg-remux non-Matroska files just to tag a
    track — too much risk of subtle issues (timestamp re-derivation, lost
    custom metadata, fenêtre d'écriture sur le fichier source). Instead we
    raise a clear, recoverable error with the file untouched."""
    f = tmp_path / "movie.mp4"
    f.write_bytes(b"\x00")

    with patch("subprocess.run") as run:
        with pytest.raises(track_metadata.MetadataWriteError, match="MKV/MKA/WebM only"):
            track_metadata.write_audio_language(f, 1, "fr")
        # Crucially: we should NOT have shelled out to anything for non-MKV.
        # The file must be left untouched.
        assert run.call_count == 0
    assert f.read_bytes() == b"\x00"   # untouched


def test_avi_and_mov_also_refused(tmp_path):
    """Same protection for other non-Matroska containers."""
    for ext in (".avi", ".mov", ".m4v", ".ts"):
        f = tmp_path / f"movie{ext}"
        f.write_bytes(b"\x00")
        with patch("subprocess.run") as run:
            with pytest.raises(track_metadata.MetadataWriteError, match="MKV/MKA/WebM only"):
                track_metadata.write_audio_language(f, 1, "fr")
            assert run.call_count == 0


def test_non_audio_stream_raises(tmp_path):
    """If the caller passes an absolute index that doesn't correspond to an
    audio stream (e.g. they passed a video index by mistake), we should
    refuse rather than tagging the wrong track."""
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"\x00")

    def run_stub(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return _fake_ffprobe([1, 2])  # only audio streams 1 and 2
        raise AssertionError(f"unexpected: {cmd[0]}")

    with patch("subprocess.run", side_effect=run_stub):
        with pytest.raises(track_metadata.MetadataWriteError, match="not an audio stream"):
            track_metadata.write_audio_language(f, 0, "fr")


def test_mkvpropedit_failure_raises(tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"\x00")

    def run_stub(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return _fake_ffprobe([1])
        if cmd[0] == "mkvpropedit":
            return _failed_proc("permission denied")
        raise AssertionError(cmd[0])

    with patch("subprocess.run", side_effect=run_stub):
        with pytest.raises(track_metadata.MetadataWriteError, match="mkvpropedit exit 1"):
            track_metadata.write_audio_language(f, 1, "fr")


def test_mkvpropedit_missing_raises_clearly(tmp_path):
    """If mkvtoolnix isn't installed we should give a clear message rather
    than letting FileNotFoundError leak."""
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"\x00")

    def run_stub(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return _fake_ffprobe([1])
        if cmd[0] == "mkvpropedit":
            raise FileNotFoundError(2, "No such file", "mkvpropedit")
        raise AssertionError(cmd[0])

    with patch("subprocess.run", side_effect=run_stub):
        with pytest.raises(track_metadata.MetadataWriteError, match="mkvtoolnix-cli"):
            track_metadata.write_audio_language(f, 1, "fr")
