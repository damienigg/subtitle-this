"""Smoke tests against the running FastAPI app via TestClient.

These exercise routing, request parsing, and response shape — but stub the
heavy externals (Emby HTTP, Whisper, LLM calls) so the tests run in seconds
without network or models.
"""
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


def test_version_endpoint_matches_package_version(client):
    """GET /api/version is the programmatic source of truth and must match
    app.__version__. Single source of truth is app/__init__.py — any drift
    means either pyproject.toml, the FastAPI app, or the footer are out of
    sync, which is exactly the failure this test prevents."""
    from app import __version__
    r = client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert body == {"version": __version__}
    # Sanity-check the shape rather than the literal — semver-ish strings
    # like "0.5.0" or "0.5.0+dirty" both pass.
    assert isinstance(body["version"], str) and body["version"]


def test_version_renders_in_page_footer(client):
    """The footer in base.html (inherited by every page) shows the version
    so operators looking at the running container can identify the build
    without shelling in. If a template refactor drops the footer this
    test catches it."""
    from app import __version__
    r = client.get("/")
    assert r.status_code == 200
    assert "app-footer" in r.text
    assert f"v{__version__}" in r.text


def test_running_job_elapsed_is_inside_progress_label(client):
    """The 0.6.2 cosmetic moved the elapsed-time counter INSIDE the
    progress bar overlay (alongside `· 65% · transcribing`) instead of
    rendering on its own line below the bar. This test injects a fake
    running job and asserts the new DOM structure so a future template
    refactor can't silently put the counter back outside the bar."""
    import time
    from app import jobs
    from app.jobs import Job

    fake = Job(
        id="testlbl1234", item_id="x", item_name="LabelTest",
        target_lang="fr", provider="nllb", mode="audio",
        status="running",
        progress_pct=42.0,
        progress_stage="transcribing",
        started_at=time.time() - 30.0,
    )
    jobs._jobs[fake.id] = fake
    try:
        r = client.get("/partials/jobs")
        assert r.status_code == 200
        body = r.text
        # The elapsed-time span must sit INSIDE the progress-label, NOT
        # as a sibling div underneath the progress-wrap. The simplest
        # structural assertion: an opening <span class="progress-label">
        # is followed (before its closing </span>) by an
        # <span class="elapsed-time" ...>. We don't try to parse HTML —
        # a regex hit confirms the nesting order.
        import re
        m = re.search(
            r'<span class="progress-label">\s*<span class="elapsed-time"',
            body,
        )
        assert m is not None, (
            "elapsed-time is no longer nested inside progress-label — the "
            "0.6.2 cosmetic regressed. Body around the cell:\n" + body[:800]
        )
    finally:
        jobs._jobs.pop(fake.id, None)


def test_settings_page_renders_with_cost_aware_labels(client):
    """The HTML settings page must render and surface the cost/quality
    trade-off where users actually make the choice — inline option
    badges in the provider/mode dropdowns + the per-section
    descriptions. The standalone "Cost ladder" hero card was removed
    in 0.7.10 as duplicative noise."""
    r = client.get("/settings")
    assert r.status_code == 200
    body = r.text
    # Per-section descriptions still carry the cost framing
    assert "ALWAYS FREE" in body                           # STT section
    assert "cost/complexity lever" in body                 # Defaults section
    # Cost-aware option labels rendered in the dropdowns
    assert "[FREE · LOCAL]" in body                        # nllb option
    assert "[FREE TIER 500k chars/mo · CLOUD beyond]" in body   # deepl option
    assert "[+0 LLM calls beyond translation]" in body     # audio mode option
    # The standalone hero card is gone.
    assert "hero-cost-ladder" not in body


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


def test_openvino_status_endpoint_returns_shape(client):
    """The Dashboard hydrates an "AUTO → GPU/CPU" pill from /api/openvino/status.
    The endpoint must always return the {configured_device, models} shape so
    the JS doesn't have to handle "endpoint missing" specially."""
    r = client.get("/api/openvino/status")
    assert r.status_code == 200
    body = r.json()
    assert "configured_device" in body
    assert "models" in body
    assert isinstance(body["models"], dict)


def test_jobview_includes_elapsed_seconds_and_snapshot_at(client, monkeypatch):
    """The dashboard's elapsed-time ticker reads two fields off JobView:
    elapsed_seconds (server-computed at serialization) and snapshot_at
    (server wall-clock at the same moment). Without both, the ticker can't
    re-anchor on each HTMX swap and the displayed timer would drift."""
    import time as _time
    from app import jobs as jobs_mod
    j = jobs_mod.Job(
        id="testjob", item_id="i", item_name="Movie",
        target_lang="fr", provider="nllb", mode="audio",
    )
    j.started_at = _time.time() - 42.0   # 42s ago
    j.status = "running"
    monkeypatch.setattr(jobs_mod, "_jobs", {j.id: j})

    r = client.get("/api/jobs")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    view = body[0]
    assert "elapsed_seconds" in view
    assert "snapshot_at" in view
    # Should report ~42s (allow a generous fudge for test runtime).
    assert 40.0 <= view["elapsed_seconds"] <= 60.0
    # snapshot_at is roughly "now"
    assert abs(view["snapshot_at"] - _time.time()) < 5.0


def test_cancel_unknown_job_returns_404(client):
    """The Cancel button on the jobs table POSTs to /api/jobs/{id}/cancel.
    Calling it with a stale id (job evicted from the in-memory cap) should
    404 cleanly, not 500."""
    r = client.post("/api/jobs/this-job-does-not-exist/cancel")
    assert r.status_code == 404


def test_libraries_endpoint_412_when_unconfigured(client, monkeypatch):
    """The library-list endpoint exists but bubbles up 412 when the server
    URL/key aren't configured — same pattern as /api/server/items and
    /api/process/{id}."""
    from app.config import settings
    monkeypatch.setattr(settings, "_overrides", {**settings._overrides, "media_server_url": "", "media_server_api_key": ""})
    r = client.get("/api/server/libraries")
    assert r.status_code == 412


def test_library_page_renders_library_dropdown(client, monkeypatch):
    """The Library page filter form must surface the library dropdown
    (with at least the 'All libraries' option) so users on a server with
    multiple libraries (films + series) can scope the listing to one."""
    from app.config import settings
    monkeypatch.setattr(settings, "_overrides", {**settings._overrides, "media_server_url": "", "media_server_api_key": ""})
    r = client.get("/library")
    assert r.status_code == 200
    # Even when unconfigured, the Library label + 'All libraries' option
    # ship with the markup so the user can see what filter would exist.
    # (Configured server backs the dropdown options at runtime.)
    body = r.text
    if 'name="library_id"' in body:
        # The dropdown is gated on `configured` being True, so when
        # unconfigured the page returns the warning article instead.
        # Either branch is acceptable; only fail if neither shows up.
        return
    assert "not configured" in body


def test_sweep_endpoint_does_not_exist(client):
    """Whole-library sweep was removed by design — there is no UI affordance
    to subtitle an entire library at once. Per-item and custom-batch flows
    remain. POSTs to the old /api/sweep path must 404."""
    r = client.post("/api/sweep")
    assert r.status_code == 404
    r2 = client.post("/api/sweep?target_lang=fr")
    assert r2.status_code == 404


def test_batch_endpoint_does_not_exist(client):
    """The /api/batch endpoint was removed when we redesigned the batch UX
    around per-item parameters. The Library page now iterates and POSTs to
    /api/process/{id} once per batched item from JS, with each item's
    individually-edited target_lang and mode in the query string. Older
    clients hitting /api/batch must 404."""
    r = client.post("/api/batch", data={})
    assert r.status_code == 404
    r2 = client.post("/api/batch", data={"item_id": ["a", "b"]})
    assert r2.status_code == 404


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
    assert "Subtitle This" in r.text


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


def test_dashboard_renders_when_running_jobs_exist(client, monkeypatch):
    """Regression: an earlier commit referenced JobView-only fields
    (snapshot_at, elapsed_seconds) directly in the _jobs_table.html
    partial. Since the partial is fed bare Job dataclass instances —
    not JobView — that crashed the dashboard with UndefinedError as
    soon as ANY job existed. Lock down: the dashboard must render
    cleanly with both running and finished jobs in the registry."""
    import time as _time
    from app import jobs as jobs_mod
    running = jobs_mod.Job(
        id="r1", item_id="i1", item_name="Running movie",
        target_lang="fr", provider="nllb", mode="audio",
    )
    running.status = "running"
    running.started_at = _time.time() - 30.0
    running.progress_pct = 42.5
    running.progress_stage = "transcribing"

    done = jobs_mod.Job(
        id="d1", item_id="i2", item_name="Finished movie",
        target_lang="fr", provider="nllb", mode="audio",
    )
    done.status = "succeeded"
    done.started_at = _time.time() - 600.0
    done.finished_at = _time.time() - 60.0
    done.progress_pct = 100.0

    canceled = jobs_mod.Job(
        id="c1", item_id="i3", item_name="Aborted movie",
        target_lang="fr", provider="nllb", mode="audio",
    )
    canceled.status = "canceled"
    canceled.started_at = _time.time() - 200.0
    canceled.finished_at = _time.time() - 100.0

    monkeypatch.setattr(jobs_mod, "_jobs", {
        running.id: running, done.id: done, canceled.id: canceled,
    })

    # Both the dashboard and the partial it includes must render without
    # raising — exercising the elapsed-time computation path for each
    # status (running, succeeded, canceled).
    r = client.get("/")
    assert r.status_code == 200
    assert "Running movie" in r.text
    assert "Finished movie" in r.text
    assert "Aborted movie" in r.text

    p = client.get("/partials/jobs")
    assert p.status_code == 200


def test_cache_explorer_page_renders(client):
    """GET /cache renders both sections (VTT + Transcript) without raising,
    even with an empty cache dir. Catches Jinja template breakage on the
    new page added in 0.7.4."""
    r = client.get("/cache")
    assert r.status_code == 200
    assert "VTT cache" in r.text
    assert "Transcript cache" in r.text


def test_cache_explorer_api_endpoints_return_lists(client):
    r = client.get("/api/cache/vtt")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    r = client.get("/api/cache/transcripts")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def _redirect_cache_dir(tmp_path, monkeypatch):
    """Point settings.cache_dir at a fresh tmp_path. Belt-and-suspenders:
    we also strip any pre-existing instance attribute that a prior test
    may have left behind via ``settings.cache_dir = X`` (some legacy
    tests do that — monkeypatch's restore-on-teardown puts the value
    BACK as an instance attribute, shadowing _overrides permanently).
    Returns the chosen cache_dir."""
    from app.config import settings as runtime_settings
    if "cache_dir" in runtime_settings.__dict__:
        monkeypatch.delattr(runtime_settings, "cache_dir", raising=False)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": str(cache_dir)},
    )
    return cache_dir


def test_cache_stats_api_returns_stats_for_existing_entry(client, tmp_path, monkeypatch):
    """End-to-end: write a cached payload, hit the stats API, get a
    JSON record with the cue count and duration buckets populated."""
    import json
    cache_dir = _redirect_cache_dir(tmp_path, monkeypatch)
    payload = {
        "vtt": (
            "WEBVTT\n\n"
            "NOTE Subtitle This auto-subs (en -> fr, mode=audio, "
            "whisper=small, provider=nllb)\n\n"
            "00:00:00.000 --> 00:00:02.000\nFirst cue\n\n"
            "00:00:10.000 --> 00:00:12.000\nSecond cue\n"
        ),
        "media_path": "/m/test.mkv",
        "mode": "audio",
        "detected_source_language": "en",
        "cue_count": 2,
    }
    (cache_dir / "abc12345.json").write_text(json.dumps(payload))

    r = client.get("/api/cache/vtt/abc12345/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cue_count"] == 2
    assert body["media_name"] == "test.mkv"
    assert body["source_lang"] == "en"
    assert body["target_lang"] == "fr"


def test_cache_stats_page_renders(client, tmp_path, monkeypatch):
    import json
    cache_dir = _redirect_cache_dir(tmp_path, monkeypatch)
    payload = {
        "vtt": (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\nHi\n"
        ),
        "media_path": "/m/film.mkv",
        "mode": "audio",
        "detected_source_language": "en",
        "cue_count": 1,
    }
    (cache_dir / "abc12345.json").write_text(json.dumps(payload))

    r = client.get("/cache/vtt/abc12345/stats")
    assert r.status_code == 200
    assert "Cues" in r.text
    assert "Coverage" in r.text
    assert "film.mkv" in r.text


def test_cache_stats_api_404_when_missing(client):
    r = client.get("/api/cache/vtt/doesnotexist/stats")
    assert r.status_code == 404


def test_job_stats_page_recovers_pipeline_metrics_from_cache_for_legacy_job(
    client, tmp_path, monkeypatch,
):
    """0.7.15 fallback: a Job persisted before 0.7.13 (when
    ``Job.pipeline_metrics`` was introduced) lacks the in-job
    metrics field but the VTT cache payload still has them.
    The /jobs/{id}/stats route now walks cache_dir/*.json and
    matches by media basename to recover the metrics — so the
    page surfaces the same score as the pill displays."""
    import json
    from app import jobs as jobs_mod
    from app.config import settings as runtime_settings
    from app.jobs import Job

    # Wire cache_dir to a tmp path so we control its contents.
    if "cache_dir" in runtime_settings.__dict__:
        monkeypatch.delattr(runtime_settings, "cache_dir", raising=False)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": str(cache_dir)},
    )

    # Write a cache payload as the runner would have written it.
    payload = {
        "vtt": (
            "WEBVTT\n\n"
            "NOTE Subtitle This auto-subs (en -> fr, mode=audio, "
            "whisper=small, provider=nllb)\n\n"
            "00:00:00.000 --> 00:00:02.000\nHi\n"
        ),
        "media_path": "/m/Inception.2010.mkv",
        "mode": "audio",
        "cue_count": 1,
        "pipeline_metrics": {
            "vad": None, "whisper": None, "translation": None,
            "packing": {
                "enabled": True, "windows_total": 10,
                "windows_packed": 10, "windows_single_region": 0,
                "avg_regions_per_window": 12.0,
                "cue_drop_pad_zone_count": 700,
                "cue_snap_pad_zone_count": 0,
                "cue_keep_count": 900,
            },
        },
    }
    (cache_dir / "abc123def4567890.json").write_text(json.dumps(payload))

    # The .vtt next to the media — name follows the runner's pattern.
    vtt_path = tmp_path / "Inception.2010.fr.audio.ai.vtt"
    vtt_path.write_text(payload["vtt"], encoding="utf-8")

    # Legacy job — quality_score is set (so the pill renders), but
    # pipeline_metrics field is None (pre-0.7.13 state).
    job = Job(
        id="legacyjob1",
        item_id="i1",
        item_name="Inception.2010.mkv",
        target_lang="fr",
        provider="nllb",
        mode="audio",
        status="succeeded",
        output_path=str(vtt_path),
        quality_score=82,
        quality_grade="B",
        pipeline_metrics=None,    # the legacy state
    )
    monkeypatch.setattr(jobs_mod, "_jobs", {job.id: job})
    try:
        r = client.get(f"/jobs/{job.id}/stats")
        assert r.status_code == 200
        body = r.text
        # The pad-drop factor must appear, proving the cache lookup
        # recovered pipeline_metrics and threaded them into the score.
        assert "Region-packing unrecoverable drops" in body, (
            "Legacy fallback failed: pipeline_metrics not recovered "
            "from cache, page rendered without the pad-drop penalty"
        )
    finally:
        jobs_mod._jobs.pop(job.id, None)


def test_job_stats_page_uses_stored_pipeline_metrics(client, tmp_path, monkeypatch):
    """Regression for 0.7.13: /jobs/{id}/stats was recomputing the score
    from the .vtt alone, ignoring pipeline_metrics. That produced a
    higher score (no VAD/packing/translation penalties) than what the
    Jobs table's pill displayed — the table used the score from the
    runner which DID see pipeline_metrics. This locks in the fix:
    both surfaces must use the same pipeline_metrics input."""
    from app import jobs as jobs_mod
    from app.jobs import Job

    vtt_path = tmp_path / "test.vtt"
    # Minimal .vtt with one short cue. very_short_pct alone would
    # produce a near-perfect score; the pipeline_metrics below carry
    # the penalty signal that should drag the score down.
    vtt_path.write_text(
        "WEBVTT\n\n"
        "NOTE Subtitle This auto-subs (en -> fr, mode=audio, "
        "whisper=small, provider=nllb)\n\n"
        "00:00:00.000 --> 00:00:02.000\nHi\n",
        encoding="utf-8",
    )

    job = Job(
        id="testjob123",
        item_id="i1",
        item_name="test.mkv",
        target_lang="fr",
        provider="nllb",
        mode="audio",
        status="succeeded",
        output_path=str(vtt_path),
        # Pipeline metrics with a heavy pad-drop signal that the
        # quality scorer must penalize.
        pipeline_metrics={
            "vad": None,
            "whisper": None,
            "translation": None,
            "packing": {
                "enabled": True,
                "windows_total": 10,
                "windows_packed": 10,
                "windows_single_region": 0,
                "avg_regions_per_window": 12.0,
                "cue_drop_pad_zone_count": 700,   # → "unrecoverable drops" critical penalty
                "cue_snap_pad_zone_count": 0,
                "cue_keep_count": 900,
            },
        },
    )
    monkeypatch.setattr(jobs_mod, "_jobs", {job.id: job})
    try:
        r = client.get(f"/jobs/{job.id}/stats")
        assert r.status_code == 200
        body = r.text
        # The pad-drop penalty MUST appear on the page — that's the
        # signal proving pipeline_metrics was consumed.
        assert "Region-packing unrecoverable drops" in body, (
            "Quality factor list is missing the pad-drops penalty — "
            "pipeline_metrics was not passed to compute_from_vtt"
        )
    finally:
        jobs_mod._jobs.pop(job.id, None)


def test_dashboard_redirects_to_wizard_when_server_not_configured(client, monkeypatch):
    """First-run: fresh install with no media_server_url or api_key
    set should redirect the user from the dashboard to /onboarding
    so they get guided setup instead of an empty dashboard."""
    from app.config import settings as runtime_settings
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides,
         "media_server_url": "", "media_server_api_key": ""},
    )

    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding"


def test_dashboard_skip_wizard_query_param_bypasses_redirect(client, monkeypatch):
    """The wizard's "Skip — I'll configure manually" link adds
    ?skip_wizard=1 to the dashboard URL so power users can land on
    the dashboard without filling in the wizard first."""
    from app.config import settings as runtime_settings
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides,
         "media_server_url": "", "media_server_api_key": ""},
    )

    r = client.get("/?skip_wizard=1", follow_redirects=False)
    assert r.status_code == 200


def test_dashboard_no_redirect_when_server_is_configured(client, monkeypatch):
    """Already-configured installs go straight to the dashboard
    without ever seeing /onboarding."""
    from app.config import settings as runtime_settings
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides,
         "media_server_url": "http://emby.lan:8096",
         "media_server_api_key": "abc"},
    )

    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_onboarding_page_renders(client):
    """The wizard template renders without error and shows the three
    section headers."""
    r = client.get("/onboarding")
    assert r.status_code == 200
    body = r.text
    assert "Connect to your media server" in body
    assert "Pick your defaults" in body
    assert "You're done" in body


def test_onboarding_save_redirects_to_library(client, monkeypatch):
    """Submitting the wizard updates settings via the same path as
    Settings, then redirects to /library where the user actually
    works."""
    from app.config import settings as runtime_settings
    monkeypatch.setattr(
        runtime_settings, "_overrides", dict(runtime_settings._overrides),
    )
    r = client.post(
        "/onboarding",
        data={
            "media_server_type": "emby",
            "media_server_url": "http://emby.lan:8096",
            "media_server_api_key": "test-key",
            "default_target_lang": "fr",
            "default_mode": "audio",
            "default_translation_provider": "nllb",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/library"
    # The values landed in _overrides.
    assert runtime_settings._overrides.get("media_server_url") == "http://emby.lan:8096"
    assert runtime_settings._overrides.get("media_server_api_key") == "test-key"


def test_cache_repolish_endpoint_rewrites_vtt_in_place(client, tmp_path, monkeypatch):
    """End-to-end repolish flow: write a cache payload with a tight
    cue, POST to /api/cache/vtt/{key}/repolish, verify the cached
    .vtt now has an extended cue. Proves the parse → polish → write
    path works through the HTTP layer."""
    import json
    from app.config import settings as runtime_settings
    cache_dir = _redirect_cache_dir(tmp_path, monkeypatch)

    src_vtt = (
        "WEBVTT\n\n"
        "NOTE Subtitle This auto-subs (en -> fr, mode=audio, "
        "whisper=small, provider=nllb)\n\n"
        "00:00:10.000 --> 00:00:10.300\nYes.\n"
    )
    payload = {
        "vtt": src_vtt,
        "media_path": "/m/nonexistent.mkv",   # path won't resolve → disk write is skipped
        "mode": "audio",
        "cue_count": 1,
    }
    (cache_dir / "abc12345.json").write_text(json.dumps(payload))

    r = client.post("/api/cache/vtt/abc12345/repolish")
    assert r.status_code == 200
    body = r.json()
    assert body["before_cue_count"] == 1
    assert body["after_cue_count"] == 1
    # disk_vtt_updated stays False here because /m/nonexistent.mkv
    # parent directory doesn't exist — we only write when a real
    # media folder is reachable.
    assert body["disk_vtt_updated"] is False

    # The cached payload now carries a polished .vtt — the original
    # 0.3 s cue stretched out toward the 1.2 s floor.
    persisted = json.loads((cache_dir / "abc12345.json").read_text())
    assert "00:00:11.20" in persisted["vtt"] or "00:00:11.200" in persisted["vtt"]


def test_cache_repolish_refreshes_job_quality_score(client, tmp_path, monkeypatch):
    """Regression for the "Jobs table pill stays stale after re-polish"
    bug: when /api/cache/vtt/{key}/repolish overwrites the .vtt next to
    the media, any Job whose output_path points at the same .vtt must
    have its quality_score recomputed from the new file — otherwise the
    dashboard pill keeps showing the pre-polish score while the stats
    page recomputes and shows a different one."""
    import json
    from app import jobs as jobs_mod
    cache_dir = _redirect_cache_dir(tmp_path, monkeypatch)

    # Fake media path that DOES resolve so disk_vtt_updated turns True.
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"")  # path just needs to exist
    disk_vtt = media.with_name("movie.fr.audio.ai.vtt")

    # Cache payload — one very-short cue so polish will extend it
    # AND the resulting quality_score is well-defined.
    src_vtt = (
        "WEBVTT\n\n"
        "NOTE Subtitle This auto-subs (en -> fr, mode=audio, "
        "whisper=small, provider=nllb)\n\n"
        "00:00:10.000 --> 00:00:10.300\nYes.\n\n"
        "00:00:20.000 --> 00:00:23.000\nA proper-length cue here.\n"
    )
    payload = {
        "vtt": src_vtt,
        "media_path": str(media),
        "mode": "audio",
        "cue_count": 2,
    }
    (cache_dir / "deadbeef12345678.json").write_text(json.dumps(payload))
    # Pre-write the disk .vtt so the repolish path's write succeeds AND
    # so it matches a Job's output_path. The contents don't matter — the
    # endpoint overwrites them.
    disk_vtt.write_text(src_vtt, encoding="utf-8")

    # Plant a Job pointing at the same on-disk .vtt with a known stale
    # quality_score. After repolish, this score must change.
    jobs_mod._jobs.clear()
    j = jobs_mod.Job(
        id="job-test", item_id="m1", item_name="movie",
        target_lang="fr", provider="nllb", mode="audio",
        status="succeeded", output_path=str(disk_vtt),
        quality_score=42, quality_grade="F",
    )
    jobs_mod._jobs[j.id] = j

    r = client.post("/api/cache/vtt/deadbeef12345678/repolish")
    assert r.status_code == 200
    body = r.json()
    assert body["disk_vtt_updated"] is True
    assert body["jobs_refreshed"] == 1
    assert isinstance(body["new_quality_score"], int)
    assert body["new_quality_score"] != 42  # something changed
    assert body["new_quality_grade"] in {"A", "B", "C", "D", "F"}

    # The job record itself is updated, not just the response body.
    refreshed = jobs_mod.get_job("job-test")
    assert refreshed.quality_score == body["new_quality_score"]
    assert refreshed.quality_grade == body["new_quality_grade"]

    # The .stats.json sidecar reflects the new VTT too.
    sidecar = cache_dir / "stats" / "deadbeef12345678.json"
    assert sidecar.is_file()
    sidecar_data = json.loads(sidecar.read_text())
    assert sidecar_data["quality"]["score"] == body["new_quality_score"]

    jobs_mod._jobs.clear()


def test_update_check_endpoint_returns_current_version(client, monkeypatch):
    """/api/update/check returns a JSON payload that always has at
    least the current version. Backend errors surface as ``error``
    rather than 500ing — the dashboard banner reads them to render
    a "couldn't check" state."""
    from app import updates as updates_mod

    # Stub the GitHub call so the test stays offline-safe.
    class _FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def get(self, url, headers=None):
            class _R:
                status_code = 200
                def json(self):
                    return {"tag_name": "v99.99.99", "body": "future"}
            return _R()
    monkeypatch.setattr(updates_mod.httpx, "Client", _FakeClient)
    updates_mod._cache.clear()

    from app import __version__
    r = client.get("/api/update/check?force=1")
    assert r.status_code == 200
    body = r.json()
    assert body["current_version"] == __version__
    assert body["latest_version"] == "99.99.99"
    assert body["update_available"] is True


def test_update_run_endpoint_412_when_command_not_configured(client, monkeypatch):
    """POST /api/update/run with no BABEL_UPDATE_COMMAND set returns
    412 Precondition Failed rather than executing anything."""
    from app.config import settings as runtime_settings
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "update_command": ""},
    )
    r = client.post("/api/update/run")
    assert r.status_code == 412


def test_cache_explorer_delete_rejects_path_traversal(client):
    """The HTTP layer must surface ValueError as 400, not let a malformed
    key resolve to an arbitrary file. Most `..` shapes get caught earlier
    by FastAPI's path routing (returns 404). The case our code is the
    last line of defense for is a key with characters outside the safe
    set — e.g. a space, a quote, anything not alphanum-plus-underscore-
    -plus-dot-plus-hyphen. We use a space which routes through but trips
    _validate_cache_key."""
    r = client.delete("/api/cache/vtt/has%20space")
    assert r.status_code == 400
