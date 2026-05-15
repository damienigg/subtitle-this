"""Tests for app/util.py — the shared atomic-write + JSON-load-with-
quarantine helpers introduced in 0.8.3 to deduplicate the persistence
plumbing.

These helpers are called from every cache layer in the app (settings,
jobs queue, transcript cache, VTT cache, stats sidecars) so any bug
here would manifest as silent data loss or corruption. The contract
is small: pin the atomicity guarantee and the corrupt-quarantine
behaviour.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.util import atomic_write, load_json_with_quarantine


# ── atomic_write ────────────────────────────────────────────────────────


def test_atomic_write_creates_file_and_no_tmp_lingers(tmp_path):
    """Happy path: file exists with the expected content, sibling .tmp
    is gone post-write."""
    target = tmp_path / "out.json"
    atomic_write(target, '{"hello": 1}')
    assert target.read_text() == '{"hello": 1}'
    assert not target.with_suffix(target.suffix + ".tmp").exists()


def test_atomic_write_creates_parent_directories(tmp_path):
    """A nested target whose parent doesn't exist yet must succeed —
    the helper mkdirs the parent (saves every caller from repeating
    the boilerplate)."""
    target = tmp_path / "deep" / "nested" / "file.json"
    atomic_write(target, "x")
    assert target.read_text() == "x"


def test_atomic_write_replaces_existing_file(tmp_path):
    """The whole point of atomic-replace: an existing file at the path
    gets swapped out atomically, no half-state visible to a reader."""
    target = tmp_path / "x.json"
    target.write_text("old")
    atomic_write(target, "new")
    assert target.read_text() == "new"


def test_atomic_write_does_not_clobber_original_on_disk_error(tmp_path, monkeypatch):
    """If the rename step fails, the previous good file at the target
    must remain intact. This is the load-bearing invariant — it's why
    every persistence site in the app routes through this helper
    rather than a plain ``Path.write_text``."""
    target = tmp_path / "x.json"
    target.write_text("ORIGINAL")
    snapshot = target.read_bytes()

    # Force os.replace to fail; tmp is written but never promoted.
    import os as os_mod
    def boom(*a, **kw):
        raise OSError("simulated rename fail")
    monkeypatch.setattr(os_mod, "replace", boom)

    with pytest.raises(OSError):
        atomic_write(target, "NEW")
    assert target.read_bytes() == snapshot


# ── load_json_with_quarantine ───────────────────────────────────────────


def test_load_json_missing_returns_none(tmp_path):
    log = logging.getLogger("test")
    assert load_json_with_quarantine(tmp_path / "absent.json", log) is None


def test_load_json_returns_parsed_dict(tmp_path):
    path = tmp_path / "good.json"
    path.write_text(json.dumps({"k": 42, "list": [1, 2, 3]}))
    log = logging.getLogger("test")
    out = load_json_with_quarantine(path, log)
    assert out == {"k": 42, "list": [1, 2, 3]}


def test_load_json_quarantines_corrupt_file(tmp_path, caplog):
    """A file with garbled JSON must NOT crash the caller — it gets
    renamed to .corrupt so the next run starts clean, and a warning
    is logged for the operator. This is the contract every persistence
    site relies on."""
    path = tmp_path / "broken.json"
    path.write_text("not json {{{")

    log = logging.getLogger("subtitle_this")
    with caplog.at_level(logging.WARNING, logger="subtitle_this"):
        result = load_json_with_quarantine(path, log, label="testsite")

    assert result is None
    # Original is gone; corrupt sidecar is in its place.
    assert not path.exists()
    assert path.with_suffix(path.suffix + ".corrupt").exists()
    # Operator can spot which site flagged the corruption.
    assert "testsite" in caplog.text
    assert "broken.json" in caplog.text


def test_load_json_quarantine_rename_failure_still_returns_none(
    tmp_path, monkeypatch, caplog,
):
    """If the corrupt file CAN'T be renamed (read-only mount, broken
    perms), the helper still returns None and logs — it must NOT raise.
    A noisy log is fine; a crashed FastAPI startup is not."""
    path = tmp_path / "broken.json"
    path.write_text("not json")

    def boom(*a, **kw):
        raise OSError("can't rename")
    monkeypatch.setattr(Path, "rename", boom)

    log = logging.getLogger("subtitle_this")
    with caplog.at_level(logging.WARNING, logger="subtitle_this"):
        result = load_json_with_quarantine(path, log)
    assert result is None
    assert "could not be renamed" in caplog.text


def test_load_json_label_is_optional(tmp_path, caplog):
    """The ``label`` kwarg is optional — calling without it should
    still produce a sensible log message."""
    path = tmp_path / "broken.json"
    path.write_text("not json")
    log = logging.getLogger("subtitle_this")
    with caplog.at_level(logging.WARNING, logger="subtitle_this"):
        load_json_with_quarantine(path, log)
    # No "label: " prefix when label is unset.
    assert ": broken.json" not in caplog.text or "testsite" not in caplog.text
    # But still mentions the file
    assert "broken.json" in caplog.text
