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

from app import cache_explorer, jobs, updates as updates_mod
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

    # Fail fast when vocal_isolation_enabled is set but the demucs
    # package isn't actually importable in this image. Without this
    # check the job would queue, run for a few seconds, then fail with
    # "demucs is not installed" deep in the pipeline. Bouncing the
    # submit returns the error to the UI immediately and tells the
    # operator the actual fix (rebuild image with the vocal-isolation
    # extra, or toggle the setting off).
    if settings.vocal_isolation_enabled:
        from app.pipeline import vocal_isolation as vi
        ok, err = vi.is_available()
        if not ok:
            raise ValueError(
                "Vocal isolation is ON in Settings but `demucs` is not "
                "usable in this container. If you're on the GHCR image: "
                "`docker compose pull && docker compose up -d` to grab "
                "a build that ships the vocal-isolation extra (every "
                "image from 0.7.27 onward includes it). If you build "
                "your own image: ensure `demucs>=4.0` is installed and "
                "that `from demucs.pretrained import get_model` works. "
                "Otherwise, turn off `vocal_isolation_enabled` in "
                f"Settings. Import error: {err}"
            )

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

        # Compute the heuristic quality score and stamp it on the job
        # so the dashboard's Jobs table can show a per-run grade pill.
        # Cheap (millisecond-scale text-parse on the just-written .vtt)
        # and only runs on the success path, so a failure here can't
        # take down the surrounding job.
        try:
            from app import stats as stats_mod
            from app import quality as quality_mod
            stats_record = stats_mod.compute_from_vtt(
                result.vtt,
                media_path=str(media),
                mode=result.mode,
                detected_source_language=result.detected_source_language,
                took_seconds=result.took_seconds,
                pipeline_metrics=result.pipeline_metrics,
            )
            q = quality_mod.compute_quality_score(stats_record)
            job.quality_score = q.score
            job.quality_grade = q.grade
            # Persist the pipeline metrics on the Job too — the per-job
            # stats page (/jobs/{id}/stats) recomputes the full record
            # from these on demand and would compute a different score
            # if they were missing (it'd see only the .vtt-derived
            # signals and miss the VAD / packing / translation penalties).
            job.pipeline_metrics = result.pipeline_metrics
        except Exception:
            _log.warning("quality score computation failed", exc_info=True)

        # The .stats.json sidecar is written by processor.process()
        # into cache_dir/stats/, not here — keeping it inside the
        # cache avoids polluting the user's movie folder with
        # metrics files alongside the .vtt itself.

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

    # Resolve the translation model identifier for the active provider.
    # NLLB → nllb_model (HuggingFace ID), LLM → translation_llm_model,
    # DeepL has no per-model selection so we leave it empty.
    if provider == "nllb":
        translation_model = settings.nllb_model
    elif provider == "llm":
        translation_model = settings.translation_llm_model
    else:
        translation_model = None

    return jobs.submit(
        item_id=item.id,
        item_name=item.name,
        target_lang=target_lang,
        provider=provider,
        mode=job_mode,
        runner=runner,
        whisper_model=settings.whisper_model,
        translation_model=translation_model,
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


@router.get("/jobs/{job_id}/output.vtt")
def get_job_output(job_id: str):
    """Stream the .vtt produced by this job back to the browser as
    text/vtt. The Jobs table's Output pill links to this so clicking
    it opens the subtitle in a new tab — no need for the user to SSH
    or share-mount the NAS folder.

    Defense: we only serve the path the JOB recorded as its own
    output. A request for an arbitrary path is rejected because
    output_path is set by the runner from a server-controlled
    template (``_vtt_path(media, target_lang, mode)``), and we
    never trust a user-supplied filename here."""
    from pathlib import Path
    from fastapi.responses import Response
    j = jobs.get_job(job_id)
    if not j:
        raise HTTPException(404, f"job {job_id!r} not found")
    if not j.output_path:
        raise HTTPException(404, f"job {job_id!r} has no output yet")
    path = Path(j.output_path)
    if not path.is_file():
        raise HTTPException(
            404,
            f"output file {path.name!r} no longer exists on disk (it may "
            "have been deleted by the user or by media server housekeeping)",
        )
    try:
        body = path.read_bytes()
    except OSError as e:
        raise HTTPException(500, f"could not read output file: {e}")
    return Response(
        content=body,
        media_type="text/vtt; charset=utf-8",
        headers={
            # inline = open in-browser, NOT a forced download. The user
            # can still right-click → save-as if they want the file.
            "Content-Disposition": f'inline; filename="{path.name}"',
        },
    )


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
        pipeline_metrics=(
            payload.get("pipeline_metrics") if isinstance(payload, dict) else None
        ),
    )
    return stats_mod.to_jsonable(record)


@router.post("/cache/vtt/clear-all")
def cache_clear_all_vtt() -> dict:
    return {"cleared": cache_explorer.clear_all_vtt_entries()}


@router.post("/cache/vtt/{cache_key}/repolish")
def cache_repolish_vtt(cache_key: str) -> dict:
    """Re-apply the readability polish pass to an already-cached VTT
    without re-running STT or translation. Updates BOTH the on-disk
    cache payload AND the .vtt file next to the media (when the path
    is recoverable from the payload's ``media_path`` + the NOTE
    header's target_lang/mode). Returns the before/after cue counts
    so the UI can confirm what changed."""
    import json
    from pathlib import Path
    from app.pipeline.polish import polish_vtt_text

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
    if not isinstance(payload, dict) or not payload.get("vtt"):
        raise HTTPException(404, f"entry {cache_key!r} has no .vtt content")

    old_vtt = payload["vtt"]
    new_vtt = polish_vtt_text(old_vtt)

    # Persist the polished VTT back to the cache payload atomically.
    payload["vtt"] = new_vtt
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    import os as _os
    _os.replace(tmp, path)

    # Best-effort: also overwrite the .vtt next to the media so a
    # media-server reload picks up the polished version. The path is
    # recoverable when payload has ``media_path`` AND we can parse
    # ``target_lang`` + ``mode`` out of the NOTE header.
    disk_updated = False
    disk_path: Path | None = None
    media_path = payload.get("media_path")
    if isinstance(media_path, str) and media_path:
        import re
        m = re.search(
            r"NOTE Subtitle This auto-subs "
            r"\([a-z]{2} -> (?P<tgt>[a-z]{2}), mode=(?P<mode>[a-z]+),",
            new_vtt,
        )
        # Fall back to the payload's stored mode if the NOTE didn't
        # parse — older entries always carry mode in the payload.
        mode = (m.group("mode") if m else None) or payload.get("mode")
        tgt = m.group("tgt") if m else None
        if mode and tgt:
            media = Path(media_path)
            disk_path = media.with_name(
                f"{media.stem}.{tgt}.{mode}.ai.vtt"
            )
            if disk_path.parent.is_dir():
                try:
                    disk_path.write_text(new_vtt, encoding="utf-8")
                    disk_updated = True
                except OSError:
                    pass

    # Cue count delta — surface it so the UI can tell the operator
    # "merged N cues, extended M" in one number.
    def _count_cues(text: str) -> int:
        return len([1 for line in text.splitlines() if " --> " in line])

    # ── Refresh derived artifacts so the Jobs table pill, the
    #    cache_dir/stats/ sidecar, and the on-disk .vtt all agree.
    # Without this, the Jobs table keeps showing the original
    # pre-polish quality_score (frozen at job-completion time) while
    # the stats page recomputes from the new .vtt and reports a
    # different number — confusing and easy to miss.
    jobs_refreshed = 0
    new_score: int | None = None
    new_grade: str | None = None
    try:
        from app import stats as stats_mod
        from app import quality as quality_mod
        record = stats_mod.compute_from_vtt(
            new_vtt,
            media_path=payload.get("media_path"),
            cache_key=cache_key,
            mode=payload.get("mode"),
            detected_source_language=payload.get("detected_source_language"),
            pipeline_metrics=payload.get("pipeline_metrics"),
        )
        q = quality_mod.compute_quality_score(record)
        new_score, new_grade = q.score, q.grade

        # Update Job records that point to this .vtt. The disk path is
        # deterministic from media + target + mode, so every job that
        # wrote the same .vtt shares the same output_path string.
        if disk_updated and disk_path is not None:
            target_path = str(disk_path)
            for j in jobs.list_jobs(limit=500):
                if j.output_path == target_path:
                    j.quality_score = q.score
                    j.quality_grade = q.grade
                    jobs_refreshed += 1
            if jobs_refreshed:
                jobs._persist()

        # Rewrite the .stats.json sidecar so the cache_dir/stats/<key>.json
        # record matches the post-polish .vtt. write_cache_sidecar is
        # idempotent and atomic — it's the same function the job runner
        # uses at completion time, so the format stays consistent.
        stats_mod.write_cache_sidecar(cache_key, record)
    except Exception:
        _log.warning("post-repolish score refresh failed", exc_info=True)

    return {
        "before_cue_count": _count_cues(old_vtt),
        "after_cue_count": _count_cues(new_vtt),
        "disk_vtt_updated": disk_updated,
        "jobs_refreshed": jobs_refreshed,
        "new_quality_score": new_score,
        "new_quality_grade": new_grade,
    }


# ── App update ────────────────────────────────────────────────────────────


@router.get("/update/check")
def update_check(force: bool = False) -> dict:
    """Query GitHub Releases for the latest tag and report whether
    the running app is behind. Cached for 1 h to stay under GitHub's
    unauthenticated rate limit; pass ``?force=1`` to bypass the cache
    (used by the dashboard's "Check now" button)."""
    return updates_mod.check_for_update(force_refresh=force).to_dict()


@router.post("/update/run")
def update_run() -> dict:
    """Execute the operator-configured update command (env var
    BABEL_UPDATE_COMMAND). Returns the command's combined stdout/stderr
    plus its return code. 412 when the env var isn't set — the UI
    button is hidden in that state but we re-check defensively."""
    res = updates_mod.run_update_command()
    if not res.enabled:
        raise HTTPException(
            412,
            "Update command isn't configured. Set BABEL_UPDATE_COMMAND "
            "in your container's environment and restart to enable "
            "the one-click update button.",
        )
    return res.to_dict()


@router.post("/cache/transcripts/clear-all")
def cache_clear_all_transcripts() -> dict:
    return {"cleared": cache_explorer.clear_all_transcript_entries()}
