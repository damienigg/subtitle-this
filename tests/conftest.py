"""Test setup. Sets BABEL_ env vars BEFORE any app imports so the singleton
SettingsStore picks up an isolated cache dir + dummy credentials. Fixtures
below add per-test isolation on top of the session defaults.
"""
import os
import shutil
import tempfile

# Critical: set env BEFORE any `from app...` import in test modules. pytest
# imports conftest.py before collecting tests, so doing this at module top is
# the right place.
_SESSION_CACHE = tempfile.mkdtemp(prefix="babel-test-cache-")
os.environ["BABEL_CACHE_DIR"] = _SESSION_CACHE
os.environ.setdefault("BABEL_TRANSLATION_LLM_API_KEY", "sk-test-trans")
os.environ.setdefault("BABEL_VISION_LLM_API_KEY", "sk-test-vision")
os.environ.setdefault("BABEL_DEEPL_API_KEY", "test-deepl:fx")
os.environ.setdefault("BABEL_MEDIA_SERVER_TYPE", "emby")
os.environ.setdefault("BABEL_MEDIA_SERVER_URL", "http://media.test:9999")
os.environ.setdefault("BABEL_MEDIA_SERVER_API_KEY", "test-media-server")

import pytest


def pytest_unconfigure(config):
    shutil.rmtree(_SESSION_CACHE, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_settings_overrides(monkeypatch, tmp_path):
    """Each test starts with a clean SettingsStore overrides dict and a
    per-test settings.json path so writes don't leak between tests."""
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "_overrides", {})
    monkeypatch.setattr(_settings, "_file", tmp_path / "settings.json")
    yield


@pytest.fixture(autouse=True)
def _reset_jobs():
    """Drain the in-memory job dict between tests."""
    from app import jobs
    jobs._jobs.clear()
    yield
    jobs._jobs.clear()
