"""In-memory job tracking with serialized execution.

A single asyncio.Lock serializes the heavy work (Whisper + translation) so
back-to-back UI clicks don't thrash RAM/iGPU. Jobs are kept in memory; restart
loses in-flight state. Acceptable for now; swap for sqlite if persistence matters.

The main event loop is captured at app startup (see app/main.py:lifespan) so
that sync FastAPI handlers — which run in a threadpool worker without a loop —
can still schedule the runner via run_coroutine_threadsafe.
"""
import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable


# Cap on how many job records to keep in memory. Older completed jobs evict.
MAX_JOBS = 500


@dataclass
class Job:
    id: str
    item_id: str
    item_name: str
    target_lang: str
    provider: str
    mode: str
    status: str = "queued"           # queued | running | succeeded | failed
    error: str | None = None
    output_path: str | None = None
    cue_count: int | None = None
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# OrderedDict so we can evict oldest when MAX_JOBS is reached.
_jobs: "OrderedDict[str, Job]" = OrderedDict()
_lock = asyncio.Lock()
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called from the FastAPI lifespan so sync routes can schedule async work."""
    global _main_loop
    _main_loop = loop


def list_jobs(limit: int = 50) -> list[Job]:
    return sorted(_jobs.values(), key=lambda j: j.queued_at, reverse=True)[:limit]


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
    )
    _jobs[job.id] = job
    while len(_jobs) > MAX_JOBS:
        _jobs.popitem(last=False)

    async def _run():
        async with _lock:
            job.status = "running"
            job.started_at = time.time()
            try:
                await runner(job)
                job.status = "succeeded"
            except Exception as e:
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
            finally:
                job.finished_at = time.time()

    if _main_loop is None:
        raise RuntimeError(
            "jobs.submit() called before the main loop was registered. "
            "Make sure app.main:lifespan runs first."
        )
    asyncio.run_coroutine_threadsafe(_run(), _main_loop)
    return job
