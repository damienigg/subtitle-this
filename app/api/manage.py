"""Media-server-driven endpoints. Resolves item IDs (Emby / Jellyfin / Plex)
to filesystem paths, runs the pipeline, writes .vtt next to media, refreshes
the server's metadata so it picks up the new subtitle."""
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException
from pydantic import BaseModel


_log = logging.getLogger("subtitle_this")

from app import jobs
from app.config import settings
from app.processor import (
    BadRequest, ProcessRequest, process, validate_mode_provider_combo,
)
from app.server import (
    MediaItem,
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


class BatchResult(BaseModel):
    """Aggregate result of a multi-item submission. Returned by /api/batch.
    `submitted` and `skipped` add up to the size of the input id list;
    `job_ids` only contains the ids that landed in the queue."""
    submitted: int
    skipped: int
    job_ids: list[str]


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
        )
        out = _vtt_path(media, target_lang, result.mode)
        out.write_text(result.vtt, encoding="utf-8")
        job.output_path = str(out)
        job.cue_count = result.cue_count

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
        except MediaServerError:
            # The .vtt is on disk; refresh failure is non-fatal — the server
            # will pick it up on the next library scan regardless.
            pass

    return jobs.submit(
        item_id=item.id,
        item_name=item.name,
        target_lang=target_lang,
        provider=provider,
        mode=job_mode,
        runner=runner,
    )


# ── Routes ─────────────────────────────────────────────────────────────────────


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


@router.get("/server/items", response_model=list[ItemSummary])
def list_items(
    target_lang: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    q: str | None = None,
) -> list[ItemSummary]:
    target = target_lang or settings.default_target_lang
    try:
        page = media_server_client().list_videos(
            start_index=start_index, limit=limit, search_term=q,
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
    translation_provider: str | None = None,
    mode: str | None = None,
    skip_if_target_audio_exists: bool | None = None,
) -> JobView:
    """Queue a translation job for a media-server item. All optional params
    override the corresponding default from Settings; omitting them uses the
    configured defaults. Query-param-based so HTMX's default form-POST works
    directly."""
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
    return JobView(**job.to_dict())


@router.post("/batch", response_model=BatchResult)
def process_batch(
    item_id: list[str] = Form([]),
    target_lang: str | None = Form(None),
    mode: str | None = Form(None),
    translation_provider: str | None = Form(None),
) -> BatchResult:
    """Queue translation jobs for a user-selected batch of media-server items.

    Backs the multi-select action on the Library page: user ticks N rows,
    clicks "Subtitle selected", we receive the list of item ids as repeated
    `item_id` form fields. Every selected item is queued unconditionally —
    we don't skip items that already have a subtitle, because the user may
    be deliberately re-running with new Settings. Items whose server lookup
    fails or which fail mode/provider validation are tallied in `skipped`
    so the UI can surface that count.
    """
    if not item_id:
        raise HTTPException(400, "no item ids provided")

    server = media_server_client()
    submitted: list[str] = []
    skipped = 0
    for iid in item_id:
        try:
            item = server.get_item(iid)
        except MediaServerError:
            skipped += 1
            continue
        try:
            job = submit_item_job(
                server=server,
                item=item,
                target_lang=target_lang,
                mode=mode,
                translation_provider=translation_provider,
            )
            submitted.append(job.id)
        except ValueError:
            skipped += 1

    return BatchResult(submitted=len(submitted), skipped=skipped, job_ids=submitted)


@router.get("/jobs", response_model=list[JobView])
def list_jobs(limit: int = 50) -> list[JobView]:
    return [JobView(**j.to_dict()) for j in jobs.list_jobs(limit=limit)]


@router.get("/jobs/{job_id}", response_model=JobView)
def get_job(job_id: str) -> JobView:
    j = jobs.get_job(job_id)
    if not j:
        raise HTTPException(404, f"job {job_id!r} not found")
    return JobView(**j.to_dict())
