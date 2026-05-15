"""Tests for the settings.json migration pipeline.

The migration framework was originally untested — when we added the
write-back + version-tag mechanism in 0.7.12 we also wrote these
tests to lock in the load behavior:

- Migrations are applied to the data returned by ``_load``
- After migrations, the result is persisted to disk (so subsequent
  startups don't re-run no-op migrations)
- A ``_schema_version`` tag is written to settings.json and matches
  ``app.__version__``; not exposed to the rest of the app (it's
  provenance, not a configurable field)
- The cleanup migration drops unknown keys (residue from past renames)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config as config_mod
from app.config import _EnvSettings, SettingsStore, _drop_unknown_keys


@pytest.fixture
def fresh_store(tmp_path):
    """SettingsStore wired to a tmp_path so each test starts clean."""
    env = _EnvSettings()
    # Override cache_dir on the env model so _file resolves to tmp_path.
    env.cache_dir = tmp_path
    store = SettingsStore(env)
    return store, tmp_path / "settings.json"


# ── _drop_unknown_keys ────────────────────────────────────────────────────


def test_drop_unknown_keys_removes_dead_fields():
    """Past renames sometimes leave residue (old key stays in
    settings.json even after the rename migration). _drop_unknown_keys
    cleans those up on every load."""
    known_field = next(iter(_EnvSettings.model_fields.keys()))
    data = {
        known_field: "ok",
        "totally_dead_field": "stale",
        "another_dead": 42,
        "_schema_version": "0.7.11",   # always preserved
    }

    cleaned = _drop_unknown_keys(data)

    assert known_field in cleaned
    assert "totally_dead_field" not in cleaned
    assert "another_dead" not in cleaned
    assert cleaned["_schema_version"] == "0.7.11"


def test_drop_unknown_keys_is_idempotent():
    """Running the cleanup twice produces the same output as once."""
    data = {"default_target_lang": "fr", "junk": "x"}
    once = _drop_unknown_keys(data)
    twice = _drop_unknown_keys(once)
    assert once == twice


# ── _load() schema-version write-back ─────────────────────────────────────


def test_load_stamps_schema_version_on_first_run(fresh_store):
    """A settings.json written by a pre-0.7.12 build has no
    _schema_version field. The first load with the new mechanism
    stamps it and writes back."""
    from app import __version__
    store, path = fresh_store
    path.write_text(json.dumps({"default_target_lang": "fr"}))

    loaded = store._load()

    # _schema_version is not returned to callers (it's a provenance tag).
    assert "_schema_version" not in loaded
    assert loaded["default_target_lang"] == "fr"

    # The on-disk file now carries the version tag.
    persisted = json.loads(path.read_text())
    assert persisted["_schema_version"] == __version__


def test_load_writes_back_after_dropping_unknown_keys(fresh_store):
    """Cleanup migration changes the data — the result is written
    back so the file self-heals. Otherwise every startup re-applies
    the same cleanup."""
    store, path = fresh_store
    path.write_text(json.dumps({"default_target_lang": "fr", "dead_key": "stale"}))

    loaded = store._load()

    persisted = json.loads(path.read_text())
    assert "dead_key" not in persisted
    assert "dead_key" not in loaded
    assert persisted["default_target_lang"] == "fr"


def test_load_no_writeback_when_already_current(fresh_store):
    """An already-clean settings.json with the current schema_version
    must not be re-written (avoids gratuitous disk churn + log noise
    on every container restart)."""
    from app import __version__
    store, path = fresh_store
    payload = {"default_target_lang": "fr", "_schema_version": __version__}
    path.write_text(json.dumps(payload, sort_keys=True, indent=2))
    mtime_before = path.stat().st_mtime_ns

    # Tiny sleep is not needed — write_text + os.replace produce a
    # distinct mtime even within the same nanosecond on most kernels,
    # so just compare bytes to confirm no rewrite happened.
    bytes_before = path.read_bytes()
    store._load()
    bytes_after = path.read_bytes()

    assert bytes_before == bytes_after, (
        "settings.json was rewritten despite being already at the "
        "current schema_version — wasteful disk churn"
    )
    assert path.stat().st_mtime_ns == mtime_before


def test_load_logs_migration_when_version_advances(fresh_store, caplog):
    """A version-bump that triggers a migration should leave a clear
    log line so the operator sees what happened at container start."""
    import logging
    caplog.set_level(logging.INFO, logger="subtitle_this")
    store, path = fresh_store
    path.write_text(json.dumps({"default_target_lang": "fr", "_schema_version": "0.6.0"}))

    store._load()

    msgs = [r.message for r in caplog.records]
    assert any("migrated from schema 0.6.0" in m for m in msgs), msgs


# ── Legacy rename migrations still work ──────────────────────────────────


def test_legacy_emby_url_is_renamed_to_media_server_url(fresh_store):
    """The historical migration from `emby_url` → `media_server_url`
    must still apply for users coming from very old installs."""
    store, path = fresh_store
    path.write_text(json.dumps({
        "emby_url": "https://emby.lan",
        "emby_api_key": "abc",
    }))

    loaded = store._load()

    assert loaded["media_server_url"] == "https://emby.lan"
    assert loaded["media_server_api_key"] == "abc"
    assert loaded["media_server_type"] == "emby"   # backfilled
    assert "emby_url" not in loaded
    assert "emby_api_key" not in loaded


def test_vocal_isolation_enabled_true_migrates_to_mode_chunked(fresh_store):
    """0.7.31 collapsed the bool ``vocal_isolation_enabled`` into the
    tri-state ``vocal_isolation_mode``. ``enabled=True`` maps to
    ``"chunked"`` — the safer of the two enable options, since a user
    who had it on probably did so on a typical 6 GB cgroup that would
    OOM under the FULL mode."""
    store, path = fresh_store
    path.write_text(json.dumps({
        "vocal_isolation_enabled": True,
    }))

    loaded = store._load()

    assert loaded["vocal_isolation_mode"] == "chunked"
    assert "vocal_isolation_enabled" not in loaded


def test_vocal_isolation_enabled_false_migrates_to_mode_off(fresh_store):
    """``enabled=False`` maps to ``mode="off"`` so the user's intent
    (don't run Demucs) is preserved."""
    store, path = fresh_store
    path.write_text(json.dumps({
        "vocal_isolation_enabled": False,
    }))

    loaded = store._load()

    assert loaded["vocal_isolation_mode"] == "off"
    assert "vocal_isolation_enabled" not in loaded


def test_vocal_isolation_migration_respects_existing_mode(fresh_store):
    """If both the old bool AND the new mode are present (e.g. a user
    manually edited the file), the explicit new value wins and the
    old bool is just dropped without rewriting."""
    store, path = fresh_store
    path.write_text(json.dumps({
        "vocal_isolation_enabled": True,
        "vocal_isolation_mode": "full",
    }))

    loaded = store._load()

    assert loaded["vocal_isolation_mode"] == "full"
    assert "vocal_isolation_enabled" not in loaded
