"""In-memory job tracking with serialized execution and on-disk persistence.

A single asyncio.Lock serializes the heavy work (Whisper + translation) so
back-to-back UI clicks don't thrash RAM/iGPU. Jobs live in-memory for fast
reads, with a JSON-backed shadow copy on disk (see app/jobs_store.py) so
that:

- Completed jobs stay visible in the Recent jobs panel across restarts.
- A job that was ``running`` when uvicorn died (planned restart OR
  OOM-kill) is surfaced as ``failed`` at next startup, with its last
  known progress, instead of vanishing without a trace.

The main event loop is captured at app startup (see app/main.py:lifespan) so
that sync FastAPI handlers — which run in a threadpool worker without a loop —
can still schedule the runner via run_coroutine_threadsafe.
"""
import asyncio
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable


# Cap on how many job records to keep in memory. Older completed jobs evict.
MAX_JOBS = 500


class JobCanceled(Exception):
    """Raised inside the pipeline when the user has clicked Cancel on this
    job. Caught by the runner and translated into status='canceled'.

    Also raised by Job.check_cancel when the per-job wall-clock timeout is
    exceeded — surfaces to the user as `status='failed'` with `error`
    explaining the deadline (see _run() in submit())."""


class JobTimeout(JobCanceled):
    """Subclass so the runner can distinguish 'user canceled' (status=canceled)
    from 'deadline exceeded' (status=failed with a clear error message). Both
    abort the pipeline through the same check_cancel checkpoints, so anywhere
    that already handles JobCanceled also handles JobTimeout."""


@dataclass
class Job:
    id: str
    item_id: str
    item_name: str
    target_lang: str
    provider: str
    mode: str
    status: str = "queued"           # queued | running | succeeded | failed | canceled
    error: str | None = None
    output_path: str | None = None
    cue_count: int | None = None
    # Whisper model the job ran with — snapshotted at submission time
    # so the jobs table can show "what STT did this use" even if the
    # user changed the setting between submission and completion.
    whisper_model: str | None = None
    # Translation model — NLLB model id for the nllb provider, the
    # LLM model for the llm provider, empty for deepl (no per-model
    # selection there). Same snapshot-at-submit reasoning as
    # whisper_model.
    translation_model: str | None = None
    # Heuristic quality score (0-100) computed at job-completion time
    # from the resulting .vtt + pipeline metrics. None on jobs that
    # didn't reach the writer (failed / canceled / still running).
    quality_score: int | None = None
    quality_grade: str | None = None     # A/B/C/D/F — same source
    # Per-run pipeline telemetry (VAD / packing / whisper / translation).
    # Stored on the Job so /jobs/{id}/stats can compute the SAME score
    # the runner did — without this field the page would recompute from
    # the .vtt alone and silently inflate the score by ignoring the
    # packing pad-drop / translation duplicate / VAD-coverage penalties
    # that live in this dict.
    pipeline_metrics: dict | None = None
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress_pct: float = 0.0        # 0-100; set as the pipeline advances
    progress_stage: str = ""         # human label of what the pipeline is doing
    cancel_requested: bool = False   # set by /api/jobs/{id}/cancel
    # Wall-clock deadline in seconds since epoch. None disables the timeout.
    # Set by submit() from settings.job_timeout_seconds at the moment the
    # job actually starts running (so queued time isn't counted against it).
    deadline: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def update_progress(self, pct: float, stage: str) -> None:
        # Clamp + monotonic enforcement: a stage that reports a smaller pct
        # than where we already are shouldn't make the bar move backwards.
        clamped = max(0.0, min(100.0, float(pct)))
        if clamped > self.progress_pct or stage != self.progress_stage:
            self.progress_pct = max(self.progress_pct, clamped)
            self.progress_stage = stage
            # Throttled disk shadow so a kill-mid-flight leaves a trace of
            # which percentage / stage we were at. Save sites for status
            # *transitions* use _persist() directly to bypass the throttle.
            _persist_throttled(self.id)

    def request_cancel(self) -> None:
        self.cancel_requested = True
        # Show "canceling" immediately so the UI reflects intent even before
        # the pipeline reaches its next checkpoint and actually bails.
        if self.status == "running":
            self.status = "canceling"

    def check_cancel(self) -> None:
        """Raise if the user requested cancel OR the wall-clock deadline has
        passed. Called at every pipeline checkpoint (between segments,
        between batches, between scene-detection ffmpeg lines), so deadline
        enforcement uses the same code paths as user-initiated cancel and
        adds zero overhead to the hot path beyond a `time.time()` call.
        """
        if self.cancel_requested:
            raise JobCanceled(f"job {self.id} canceled by user")
        if self.deadline is not None and time.time() > self.deadline:
            # Latch by setting cancel_requested so subsequent calls also
            # bail consistently — without this, work between two
            # check_cancel calls could continue past the deadline if
            # `time.time()` happens to dip back below in some clock-skew
            # edge case (it doesn't on Linux, but defense in depth).
            self.cancel_requested = True
            raise JobTimeout(
                f"job {self.id} exceeded wall-clock timeout"
            )


# OrderedDict so we can evict oldest when MAX_JOBS is reached.
_jobs: "OrderedDict[str, Job]" = OrderedDict()
_lock = asyncio.Lock()
# Guards _jobs against concurrent mutation. submit() runs on the threadpool
# (sync FastAPI routes), _run() runs on the event loop, and the runner
# updates Job fields on the loop too — so any structural change to the
# OrderedDict (insert + evict-oldest) takes this lock.
_jobs_lock = threading.Lock()
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called from the FastAPI lifespan so sync routes can schedule async work."""
    global _main_loop
    _main_loop = loop


def _persist() -> None:
    """Snapshot _jobs under the lock, then call jobs_store.save_jobs OUTSIDE
    the lock — disk IO must not block the in-memory queue.

    Called on every status transition. Lazy-import jobs_store so the
    persistence layer stays optional at import time (and to avoid the
    obvious cycle since jobs_store deserializes back into Job)."""
    from app import jobs_store
    with _jobs_lock:
        snapshot = list(_jobs.values())
    jobs_store.save_jobs(snapshot)


def _persist_throttled(job_id: str) -> None:
    """Throttled variant for progress-only updates — at most one write
    every PROGRESS_SAVE_INTERVAL_S seconds per job. Status transitions
    must NOT use this (they should reach disk immediately)."""
    from app import jobs_store
    with _jobs_lock:
        snapshot = list(_jobs.values())
    jobs_store.save_jobs_throttled(snapshot, job_id)


def load_persisted() -> None:
    """Read persisted jobs from disk into the in-memory dict, marking
    orphans as failed. Called once from app.main:lifespan at startup.

    Idempotent — safe to call again, though there's no production
    workflow that should need to.
    """
    from app import jobs_store
    loaded = jobs_store.load_jobs()
    if not loaded:
        return
    with _jobs_lock:
        for job in loaded:
            _jobs[job.id] = job
        while len(_jobs) > MAX_JOBS:
            _jobs.popitem(last=False)
    # Commit the orphan markers (status='failed' etc.) to disk so next
    # restart doesn't re-mark them with a fresh "process restarted at..."
    # timestamp.
    _persist()


def list_jobs(limit: int = 50) -> list[Job]:
    # Snapshot under the lock — sorting/iterating an OrderedDict that's
    # being mutated raises RuntimeError. Job dataclasses themselves can
    # mutate (status/progress) but that's a benign tear, not a structural
    # one.
    with _jobs_lock:
        snapshot = list(_jobs.values())
    return sorted(snapshot, key=lambda j: j.queued_at, reverse=True)[:limit]


def clear_finished_jobs() -> int:
    """Drop every job whose status is terminal (succeeded / failed / canceled)
    from the in-memory dict and persist the trimmed list. Returns the count of
    jobs removed.

    Running, queued, and canceling jobs are preserved — clearing them mid-flight
    would orphan the runner coroutine. The user can hit cancel first, then clear
    once the job lands in `canceled`.

    Called by the dashboard's "Clear finished jobs" button so users can keep
    the persistent jobs table from growing unbounded across weeks of runs."""
    terminal = {"succeeded", "failed", "canceled"}
    with _jobs_lock:
        to_drop = [jid for jid, j in _jobs.items() if j.status in terminal]
        for jid in to_drop:
            del _jobs[jid]
    if to_drop:
        _persist()
    return len(to_drop)


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def submit(
    *,
    item_id: str,
    item_name: str,
    target_lang: str,
    provider: str,
    mode: str,
    runner: Callable[[Job], Awaitable[None]],
    whisper_model: str | None = None,
    translation_model: str | None = None,
) -> Job:
    """Queue a job; runner does the actual work and updates the job in place.

    Safe to call from sync (threadpool) routes — schedules onto the main loop
    via run_coroutine_threadsafe.
    """
    job = Job(
        id=uuid.uuid4().hex[:12],
        item_id=item_id,
        item_name=item_name,
        target_lang=target_lang,
        provider=provider,
        mode=mode,
        whisper_model=whisper_model,
        translation_model=translation_model,
    )
    with _jobs_lock:
        _jobs[job.id] = job
        while len(_jobs) > MAX_JOBS:
            _jobs.popitem(last=False)
    # Persist the new queued row immediately so a crash between submit
    # and run still leaves a trace of "this job was asked for".
    _persist()

    async def _run():
        async with _lock:
            # Honor a cancel that came in while the job sat queued (the lock
            # serializes jobs, so a long batch can leave items waiting for
            # minutes). Don't even start.
            if job.cancel_requested:
                job.status = "canceled"
                job.finished_at = time.time()
                _persist()
                return
            job.status = "running"
            job.started_at = time.time()
            # Set the deadline once the job actually starts running so
            # queue time isn't counted against it. Lazy-import settings to
            # avoid a top-level cycle (config → nothing today, but the
            # pattern is cheap and durable).
            from app.config import settings
            timeout = int(getattr(settings, "job_timeout_seconds", 0) or 0)
            job.deadline = job.started_at + timeout if timeout > 0 else None
            job.update_progress(0, "starting")
            _persist()   # transition queued → running
            try:
                await runner(job)
                job.status = "succeeded"
                job.update_progress(100, "done")
            except JobTimeout as e:
                # Timeout is a failure, not a user-cancel — surface the
                # reason so the UI shows what happened.
                job.status = "failed"
                job.error = f"timeout: {e}"
            except JobCanceled:
                job.status = "canceled"
            except Exception as e:
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
            finally:
                job.finished_at = time.time()
                _persist()   # commit terminal state to disk

    if _main_loop is None:
        raise RuntimeError(
            "jobs.submit() called before the main loop was registered. "
            "Make sure app.main:lifespan runs first."
        )
    asyncio.run_coroutine_threadsafe(_run(), _main_loop)
    return job
