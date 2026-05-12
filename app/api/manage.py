"""Media-server-driven endpoints. Resolves item IDs (Emby / Jellyfin / Plex)
to filesystem paths, runs the pipeline, writes .vtt next to media, refreshes
the server's metadata so it picks up the new subtitle."""
import asyncio
import logging
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


_log = logging.getLogger("subtitle_this")

from app import cache_explorer, jobs
from app.config import settings
from app.processor import (
    BadRequest, ProcessRequest, process, validate_mode_provider_combo,
)
from app.server import (
    MediaItem,
    MediaLibrary,
    MediaServerClient,
    MediaServerError,
    media_server_client as _build_media_server_client,
)


router = APIRouter(prefix="/api")


def media_server_client() -> MediaServerClient:
    """Build a fresh client for the configured media server (Emby / Jellyfin /
    Plex). Raises 412 if the server isn't configured."""
    try:
        return _build_media_server_client()
    except MediaServerError as e:
        raise HTTPException(412, str(e)) from e


def _vtt_path(media: Path, target_lang: str, mode: str) -> Path:
    return media.with_name(f"{media.stem}.{target_lang}.{mode}.ai.vtt")


# ── Schemas ────────────────────────────────────────────────────────────────────


class ItemSummary(BaseModel):
    id: str
    name: str
    path: str
    type: str
    has_target_subtitle: bool


class LibrarySummary(BaseModel):
    id: str
    name: str
    type: str


class JobView(BaseModel):
    id: str
    item_id: str
    item_name: str
    target_lang: str
    provider: str
    mode: str
    status: str
    error: str | None
    output_path: str | None
    cue_count: int | None
    queued_at: float
    started_at: float | None
    finished_at: float | None
    progress_pct: float = 0.0
    progress_stage: str = ""
    cancel_requested: bool = False
    # Server-computed snapshot of elapsed seconds at the moment this view
    # was serialized. The client uses this as a base and ticks +1s locally
    # between API polls so the displayed elapsed stays smooth.
    elapsed_seconds: float = 0.0
    # The server's wall-clock time when this view was built, in unix
    # seconds. Lets the client compute "how stale is this snapshot" and
    # add the local delta — robust to small browser/server clock skew.
    snapshot_at: float = 0.0


def _job_view(job: jobs.Job) -> JobView:
    """Build a JobView with the elapsed-seconds snapshot computed at this
    instant. For finished jobs, elapsed is the final duration; for running
    or canceling, it ticks. For queued, it's 0 (the timer starts when the
    job actually begins running, not when it's submitted)."""
    snapshot_at = time.time()
    if job.started_at is None:
        elapsed = 0.0
    elif job.finished_at is not None:
        elapsed = max(0.0, job.finished_at - job.started_at)
    else:
        elapsed = max(0.0, snapshot_at - job.started_at)
    return JobView(**job.to_dict(), elapsed_seconds=elapsed, snapshot_at=snapshot_at)


# ── Shared submission helper ───────────────────────────────────────────────────


def submit_item_job(
    *,
    server: MediaServerClient,
    item: MediaItem,
    target_lang: str | None = None,
    translation_provider: str | None = None,
    source_lang_priority: list[str] | None = None,
    mode: str | None = None,
    skip_if_target_audio_exists: bool | None = None,
) -> jobs.Job:
    """Queue a job for a media-server item. Used by both UI flows (per-item
    "Subtitle this" button and the multi-select batch action) — both share
    the same defaults-from-settings fallback semantics."""
    if not item.path:
        raise ValueError(f"item {item.id!r} has no path field")

    target_lang = target_lang or settings.default_target_lang
    provider = translation_provider or settings.default_translation_provider
    source_priority = source_lang_priority or settings.default_source_lang_priority
    job_mode = mode or settings.default_mode
    skip_if_target = (
        skip_if_target_audio_exists
        if skip_if_target_audio_exists is not None
        else settings.default_skip_if_target_audio_exists
    )

    # Fail fast on mode/provider mismatch so the UI surfaces the error at
    # submission rather than after the job briefly queues and then fails.
    # validate_mode_provider_combo is the single source of truth — also
    # called inside process() defensively. Raises BadRequest; we re-raise
    # as ValueError since this function's callers (UI routes) catch
    # ValueError → HTTP 422.
    try:
        validate_mode_provider_combo(job_mode, provider)
    except BadRequest as e:
        raise ValueError(str(e)) from e

    media = Path(item.path)
    item_id = item.id

    async def runner(job: jobs.Job) -> None:
        # process() is synchronous and CPU/IO-heavy (Whisper transcription
        # alone runs 20+ min on a film). Park it on a worker thread so the
        # event loop stays free for HTMX polling, /partials/jobs auto-
        # refresh, server health probes, and concurrent UI clicks.
        result = await asyncio.to_thread(
            process,
            ProcessRequest(
                media_path=str(media),
                target_lang=target_lang,
                source_lang_priority=source_priority,
                translation_provider=provider,
                mode=job_mode,
                skip_if_target_audio_exists=skip_if_target,
            ),
            progress=job.update_progress,
            check_cancel=job.check_cancel,
        )
        out = _vtt_path(media, target_lang, result.mode)
        out.write_text(result.vtt, encoding="utf-8")
        job.output_path = str(out)
        job.cue_count = result.cue_count

        # Write the {vtt_path}.stats.json sidecar so the run's quality /
        # coverage numbers travel with the .vtt itself — copy the .vtt
        # off the NAS and the metrics come along. Best-effort: any IO
        # failure is logged and swallowed inside write_sidecar so a
        # broken metrics write can't hold the job hostage.
        from app import stats as stats_mod
        stats_record = stats_mod.compute_from_vtt(
            result.vtt,
            media_path=str(media),
            mode=result.mode,
            detected_source_language=result.detected_source_language,
            took_seconds=result.took_seconds,
        )
        stats_mod.write_sidecar(out, stats_record)

        # Language tag write-back: if the source track had no language tag and
        # Whisper detected one, persist that detection to the file's audio
        # stream metadata so the media server (and any other tool) sees the
        # right language on next probe. Best-effort — we don't fail the job
        # if it doesn't land, since the .vtt is already written.
        if (
            result.source_track_language is None
            and result.detected_source_language
            and settings.write_detected_language_to_file
        ):
            from app.pipeline import track_metadata
            try:
                track_metadata.write_audio_language(
                    media, result.source_track_index, result.detected_source_language
                )
            except track_metadata.MetadataWriteError as e:
                # Logged to stderr (visible via `docker logs`). The job
                # itself stays in 'succeeded' since the user got their .vtt.
                _log.warning("tag write-back failed for %s: %s", media, e)

        try:
            server.refresh_item(item_id)
        except MediaServerError as e:
            # The .vtt is on disk; refresh failure is non-fatal — the server
            # will pick it up on the next library scan regardless. Log at
            # WARNING so operators debugging "why didn't Emby pick up my
            # new subtitle" can see this in `docker logs` rather than
            # having to guess.
            _log.warning(
                "media-server refresh for item %s failed (subtitle is still "
                "on disk; server will pick it up on the next library scan): %s",
                item_id, e,
            )

    return jobs.submit(
        item_id=item.id,
        item_name=item.name,
        target_lang=target_lang,
        provider=provider,
        mode=job_mode,
        runner=runner,
    )


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.get("/openvino/status")
def openvino_status() -> dict:
    """What device(s) the OpenVINO AUTO plugin actually picked for each
    loaded model. Empty `models` dict means no inference has run yet since
    boot — until then, AUTO's pick is unknown by definition."""
    from app.pipeline.openvino_introspect import selected_devices_snapshot
    return {
        "configured_device": settings.openvino_device,
        "models": selected_devices_snapshot(),
    }


@router.get("/server/health")
def server_health() -> dict:
    """Probe the configured media server. Returns configured=False when no
    URL/key is set in Settings, otherwise reports the reachability."""
    if not settings.media_server_url or not settings.media_server_api_key:
        return {"configured": False, "reachable": False, "type": settings.media_server_type}
    try:
        reachable = _build_media_server_client().health()
    except MediaServerError:
        reachable = False
    return {"configured": True, "reachable": reachable, "type": settings.media_server_type}


@router.get("/server/libraries", response_model=list[LibrarySummary])
def list_libraries() -> list[LibrarySummary]:
    """The configured media server's top-level video libraries. Used by the
    Library page to populate its library-filter dropdown."""
    try:
        libs = media_server_client().list_libraries()
    except MediaServerError as e:
        raise HTTPException(502, f"Media server libraries lookup failed: {e}") from e
    return [LibrarySummary(id=l.id, name=l.name, type=l.type) for l in libs]


@router.get("/server/items", response_model=list[ItemSummary])
def list_items(
    target_lang: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    q: str | None = None,
    library_id: str | None = None,
) -> list[ItemSummary]:
    target = target_lang or settings.default_target_lang
    try:
        page = media_server_client().list_videos(
            start_index=start_index, limit=limit, search_term=q,
            library_id=library_id,
        )
    except MediaServerError as e:
        raise HTTPException(502, f"Media server request failed: {e}") from e
    return [
        ItemSummary(
            id=it.id,
            name=it.name,
            path=it.path,
            type=it.type,
            has_target_subtitle=it.has_subtitle_track(target),
        )
        for it in page.items
    ]


@router.post("/process/{item_id}", response_model=JobView)
def process_item(
    item_id: str,
    target_lang: str | None = None,
    translation_provider: Literal["nllb", "deepl", "llm"] | None = None,
    mode: Literal["audio", "scene", "cinematic"] | None = None,
    skip_if_target_audio_exists: bool | None = None,
) -> JobView:
    """Queue a translation job for a media-server item. All optional params
    override the corresponding default from Settings; omitting them uses the
    configured defaults. Query-param-based so HTMX's default form-POST works
    directly.

    `mode` and `translation_provider` are Literal-typed so FastAPI rejects
    garbage values at schema validation (422 with a helpful enum list)
    rather than letting them propagate into the pipeline where they'd
    surface as a less-readable BadRequest. target_lang stays a free string
    — coverage varies per provider (NLLB ~30 langs, DeepL ~30, LLMs
    arbitrary), so pinning it to a Literal would be the wrong cut."""
    try:
        server = media_server_client()
        item = server.get_item(item_id)
    except MediaServerError as e:
        raise HTTPException(502, f"Media server item lookup failed: {e}") from e

    try:
        job = submit_item_job(
            server=server,
            item=item,
            target_lang=target_lang,
            translation_provider=translation_provider,
            mode=mode,
            skip_if_target_audio_exists=skip_if_target_audio_exists,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _job_view(job)


@router.get("/jobs", response_model=list[JobView])
def list_jobs(limit: int = 50) -> list[JobView]:
    return [_job_view(j) for j in jobs.list_jobs(limit=limit)]


@router.get("/jobs/{job_id}", response_model=JobView)
def get_job(job_id: str) -> JobView:
    j = jobs.get_job(job_id)
    if not j:
        raise HTTPException(404, f"job {job_id!r} not found")
    return _job_view(j)


@router.post("/jobs/{job_id}/cancel", response_model=JobView)
def cancel_job(job_id: str) -> JobView:
    """Mark a job for cancellation. The pipeline checks the flag at stage
    boundaries (and between transcription segments / translation batches),
    so cancel takes effect within seconds for short stages and at the end
    of the current chunk for the long ones. Already-finished jobs return
    unchanged (no error)."""
    j = jobs.get_job(job_id)
    if not j:
        raise HTTPException(404, f"job {job_id!r} not found")
    j.request_cancel()
    return _job_view(j)


@router.post("/jobs/clear-finished")
def clear_finished_jobs() -> dict:
    """Remove all jobs in terminal states (succeeded / failed / canceled) from
    both the in-memory list and the on-disk persistence. Running, queued, and
    canceling jobs are left alone — clearing those mid-flight would orphan the
    runner coroutine.

    Returns ``{"cleared": N}`` where N is how many entries were removed."""
    n = jobs.clear_finished_jobs()
    return {"cleared": n}


# ── Cache Explorer ────────────────────────────────────────────────────────


@router.get("/cache/vtt")
def cache_list_vtt() -> list[dict]:
    """List every entry in the VTT (result) cache. One row per file on
    disk — entries written under both the quick-fp and content-fp keys
    appear twice, which the UI surfaces as a 2-line group."""
    return [e.to_dict() for e in cache_explorer.list_vtt_entries()]


@router.get("/cache/transcripts")
def cache_list_transcripts() -> list[dict]:
    """List every entry in the transcript (STT) cache."""
    return [e.to_dict() for e in cache_explorer.list_transcript_entries()]


@router.delete("/cache/vtt/{cache_key}")
def cache_delete_vtt(cache_key: str) -> dict:
    """Delete one VTT cache entry. The cache_key is the .json filename
    stem (e.g. ``0c5fd2e47d4d2aa20bef9fc4``). 404 if it's not there,
    400 if the key shape is suspicious (rejects path-traversal)."""
    try:
        removed = cache_explorer.delete_vtt_entry(cache_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not removed:
        raise HTTPException(404, f"VTT cache entry {cache_key!r} not found")
    return {"deleted": cache_key}


@router.delete("/cache/transcripts/{cache_key}")
def cache_delete_transcript(cache_key: str) -> dict:
    try:
        removed = cache_explorer.delete_transcript_entry(cache_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not removed:
        raise HTTPException(404, f"transcript cache entry {cache_key!r} not found")
    return {"deleted": cache_key}


@router.get("/cache/vtt/{cache_key}/stats")
def cache_vtt_stats(cache_key: str) -> dict:
    """Compute the quality / coverage stats for one cached entry.

    The stats are derived from the cached .vtt content on every call —
    cheap enough (few ms even on a 2 h film) and means we don't have to
    migrate old payloads. The same record is also written as a sidecar
    next to the .vtt at job-completion time; this endpoint is the source
    of truth for the Cache Explorer's stats page."""
    import json
    from pathlib import Path
    from app import stats as stats_mod

    try:
        cache_explorer._validate_cache_key(cache_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    path = Path(settings.cache_dir) / f"{cache_key}.json"
    if not path.is_file():
        raise HTTPException(404, f"VTT cache entry {cache_key!r} not found")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(500, f"unreadable cache entry: {e}")
    vtt_text = payload.get("vtt") if isinstance(payload, dict) else None
    if not isinstance(vtt_text, str) or not vtt_text:
        raise HTTPException(404, f"entry {cache_key!r} has no .vtt content")
    record = stats_mod.compute_from_vtt(
        vtt_text,
        media_path=payload.get("media_path") if isinstance(payload, dict) else None,
        cache_key=cache_key,
        mode=payload.get("mode") if isinstance(payload, dict) else None,
        detected_source_language=(
            payload.get("detected_source_language") if isinstance(payload, dict) else None
        ),
    )
    return stats_mod.to_jsonable(record)


@router.post("/cache/vtt/clear-all")
def cache_clear_all_vtt() -> dict:
    return {"cleared": cache_explorer.clear_all_vtt_entries()}


@router.post("/cache/transcripts/clear-all")
def cache_clear_all_transcripts() -> dict:
    return {"cleared": cache_explorer.clear_all_transcript_entries()}
