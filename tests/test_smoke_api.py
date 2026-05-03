"""Smoke tests against the running FastAPI app via TestClient.

These exercise routing, request parsing, and response shape — but stub the
heavy externals (Emby HTTP, Whisper, LLM calls) so the tests run in seconds
without network or models.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_settings_page_renders_with_cost_ladder(client):
    """The HTML settings page must render without errors and surface the
    cost-ladder hero + per-section descriptions that guide users from the
    free default to more expensive tiers."""
    r = client.get("/settings")
    assert r.status_code == 200
    body = r.text
    # Hero block with the cost ladder
    assert "Cost ladder" in body
    assert "NLLB" in body and "DeepL" in body and "LLM" in body
    # Per-section descriptions
    assert "ALWAYS FREE" in body                           # STT section
    assert "cost/complexity lever" in body                 # Defaults section
    # Cost-aware option labels rendered in the dropdowns
    assert "[FREE · LOCAL]" in body                        # nllb option
    assert "[FREE TIER 500k chars/mo · CLOUD beyond]" in body   # deepl option
    assert "[+0 LLM calls beyond translation]" in body     # audio mode option


def test_settings_get_masks_sensitive(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert "values" in body
    assert "sensitive" in body
    # Sensitive fields are either "[set]" or None — never the raw value
    for k in body["sensitive"]:
        v = body["values"].get(k)
        assert v in ("[set]", None), f"{k} leaked a raw value: {v!r}"


def test_settings_patch_validates_unknown_field(client):
    r = client.patch("/api/settings", json={"not_a_field": 1})
    assert r.status_code == 400


def test_settings_patch_validates_value_type(client):
    r = client.patch("/api/settings", json={"max_line_chars": "not-an-int"})
    assert r.status_code == 400


def test_settings_patch_round_trip(client):
    r = client.patch("/api/settings", json={"max_line_chars": 50})
    assert r.status_code == 200
    r2 = client.get("/api/settings")
    assert r2.json()["values"]["max_line_chars"] == 50


def test_settings_delete_resets_all(client):
    client.patch("/api/settings", json={"max_line_chars": 99})
    r = client.delete("/api/settings")
    assert r.status_code == 200
    # Default value is 42
    r2 = client.get("/api/settings")
    assert r2.json()["values"]["max_line_chars"] == 42


def test_jobs_list_initially_empty(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert r.json() == []


def test_server_health_when_unconfigured_reports_not_configured(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "_overrides", {**settings._overrides, "media_server_url": "", "media_server_api_key": ""})
    r = client.get("/api/server/health")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["reachable"] is False
    # The server type is reported even when unconfigured so the UI can
    # show "Emby (not configured)" instead of just "(not configured)".
    assert "type" in body


def test_process_endpoint_412_when_server_unconfigured(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "_overrides", {**settings._overrides, "media_server_url": "", "media_server_api_key": ""})
    r = client.post("/api/process/some-item-id")
    assert r.status_code == 412


def test_sweep_endpoint_412_when_server_unconfigured(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "_overrides", {**settings._overrides, "media_server_url": "", "media_server_api_key": ""})
    r = client.post("/api/sweep")
    assert r.status_code == 412


def test_batch_endpoint_400_when_no_items_selected(client):
    """Empty batch is a user error (button is disabled in the UI when 0
    selected, but the endpoint defends itself too)."""
    r = client.post("/api/batch", data={})
    assert r.status_code == 400


def test_batch_endpoint_412_when_server_unconfigured(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "_overrides", {**settings._overrides, "media_server_url": "", "media_server_api_key": ""})
    r = client.post("/api/batch", data={"item_id": ["a", "b"]})
    assert r.status_code == 412


def test_webhook_endpoint_does_not_exist(client):
    """Webhook receiver was removed — subtitle creation is exclusively a manual
    UI action. POSTs to the old endpoint must 404 (not 405 / not 401)."""
    r = client.post("/webhook/emby", json={"Event": "library.new", "Item": {"Id": "1"}})
    assert r.status_code == 404


def test_old_emby_namespaced_endpoints_are_gone(client):
    """When we generalized to support Jellyfin and Plex alongside Emby, the
    /api/emby/* paths got renamed to /api/server/*. Guard against accidental
    re-introduction."""
    assert client.get("/api/emby/health").status_code == 404
    assert client.get("/api/emby/items").status_code == 404


def test_transcribe_translate_endpoint_does_not_exist(client):
    """The path-based curl endpoint was removed — only the media-server-item-
    driven /api/process/{id} (UI-backed) remains."""
    r = client.post("/transcribe-translate", json={
        "media_path": "/totally/nonexistent/file.mkv",
        "target_lang": "fr",
    })
    assert r.status_code == 404


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text
    assert "Babel Tower" in r.text


def test_settings_page_renders(client):
    r = client.get("/settings")
    assert r.status_code == 200
    # Each section heading should appear
    for section in ("Translation model", "Vision model", "Speech-to-Text", "Defaults"):
        assert section in r.text


def test_library_page_renders_warning_when_unconfigured(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "_overrides", {**settings._overrides, "media_server_url": "", "media_server_api_key": ""})
    r = client.get("/library")
    assert r.status_code == 200
    assert "not configured" in r.text


def test_partials_jobs_renders(client):
    r = client.get("/partials/jobs")
    assert r.status_code == 200
    # The partial root has the auto-refresh attributes
    assert 'hx-get="/partials/jobs"' in r.text
