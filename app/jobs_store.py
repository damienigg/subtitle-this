"""On-disk persistence for the in-RAM jobs queue.

The base queue in app/jobs.py:_jobs is process-local — a uvicorn restart
(planned or forced by an OOM-kill) wipes it. From the user's perspective
that's the worst possible UX: a long-running job evaporates with no
trace, no error, no timestamp. The dashboard goes blank as if nothing
ever happened.

This module persists the queue to ``cache_dir/jobs.json`` so that:

1. Across restarts, completed/failed/canceled jobs remain visible in the
   Recent jobs panel.
2. Any job that was ``queued`` / ``running`` / ``canceling`` at the
   moment the previous process died gets surfaced as ``failed`` with a
   descriptive error — including its last known progress (e.g.
   ``"process restarted at 2026-05-11 19:42:13 ... last progress: 78%
   transcribing"``). That's the load-bearing diagnostic for OOM-kills,
   where Docker auto-restarts the container and there's otherwise no
   evidence in-app that anything was running.

Design choices:

- **JSON, not SQLite.** The queue is bounded at MAX_JOBS=500 records;
  the on-disk file stays well under 1 MB. SQLite would be overkill.

- **Atomic via ``os.replace``.** Same pattern as the settings store —
  write to ``.tmp`` then atomically rename. A power-cut mid-save leaves
  the previous good file intact, and corrupted files are renamed to
  ``.corrupt`` on load so we never silently lose the queue history.

- **Throttled progress writes.** Progress callbacks fire many times per
  second during transcription; writing the whole queue file on each
  would hammer the disk and add latency to the hot path. Status
  *transitions* (queued→running, →succeeded, etc.) write unconditionally,
  while progress-only updates use the throttled path that writes at most
  once every ``PROGRESS_SAVE_INTERVAL_S`` seconds per job.

- **Best-effort.** Persistence is for diagnostic continuity, not
  correctness. Any IO error is logged and swallowed; the in-memory queue
  remains the source of truth for the running process.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.jobs import Job


_log = logging.getLogger(__name__)

# Guards file IO. Save sites fire from both the asyncio event loop
# (where _run() runs) and the threadpool (where submit() runs) — a
# threading.Lock works from either context.
_lock = threading.Lock()

# Last wall-clock save time per job_id, used by save_jobs_throttled().
# We don't lock this dict — a torn read of a float is harmless (worst
# case we save one extra time), and we'd rather skip a small race than
# add a second lock acquisition on every progress tick.
_last_progress_save: dict[str, float] = {}

# Maximum frequency for progress-only persistence. 3s keeps the on-disk
# state useful for "died at ~78%" diagnostics without writing 10× per
# second during heavy transcription. Status transitions are NOT throttled.
PROGRESS_SAVE_INTERVAL_S = 3.0


def _store_path() -> Path:
    # Lazy import to dodge the import-cycle (jobs_store ← jobs ← config).
    from app.config import settings
    return Path(settings.cache_dir) / "jobs.json"


def _serialize(job: "Job") -> dict:
    return job.to_dict()


def _deserialize(d: dict) -> "Job":
    """Tolerant deserialize. Extra keys are dropped, missing keys take
    their Job-dataclass defaults — so a future schema bump (new optional
    field) doesn't crash startup against a pre-existing on-disk file."""
    from app.jobs import Job
    valid = {f.name for f in fields(Job)}
    kwargs = {k: v for k, v in d.items() if k in valid}
    return Job(**kwargs)


def save_jobs(jobs: list["Job"]) -> None:
    """Atomically replace the on-disk jobs file with ``jobs``.

    Called from status-transition sites (queued→running, →succeeded,
    →failed, →canceled). Errors are logged but never propagated —
    persistence is best-effort and a disk-full or permission problem
    must not crash the running pipeline.
    """
    from app.util import atomic_write
    path = _store_path()
    try:
        with _lock:
            atomic_write(path, json.dumps([_serialize(j) for j in jobs]))
    except Exception:
        _log.warning("jobs_store: failed to save %s", path, exc_info=True)


def save_jobs_throttled(jobs: list["Job"], job_id: str) -> None:
    """Throttled variant of save_jobs() for progress-update sites.

    Writes at most once every ``PROGRESS_SAVE_INTERVAL_S`` seconds per
    ``job_id``. The first call after the interval lapses triggers a
    save; intervening calls are no-ops. Status transitions should call
    save_jobs() directly to bypass this throttle.
    """
    now = time.monotonic()
    last = _last_progress_save.get(job_id, 0.0)
    if now - last < PROGRESS_SAVE_INTERVAL_S:
        return
    _last_progress_save[job_id] = now
    save_jobs(jobs)


def load_jobs() -> list["Job"]:
    """Read the persisted queue from disk and mark orphaned jobs as failed.

    Called once at app startup (in app.main:lifespan, right after
    ``set_main_loop``). Returns a list of Job dataclass instances ready
    to be re-inserted into the in-memory OrderedDict.

    Orphan rule: any job whose persisted status is ``queued``,
    ``running`` or ``canceling`` means the previous process died before
    it could reach a terminal state. These get rewritten to:

      status   = "failed"
      error    = "process restarted at <ts> before job finished
                  (likely OOM-kill or container restart) — last
                  progress: 78% transcribing"
      finished_at = now

    The orphan message is the load-bearing diagnostic the dashboard
    shows after an OOM-kill — without it, the user sees jobs in
    ``running`` state that haven't actually run for hours.

    Behavior on missing/corrupted file:
    - Missing → empty list (first boot or wiped cache).
    - Corrupted (JSON parse fails) → file is renamed to ``jobs.json.corrupt``
      so we can investigate, and we return empty rather than crashing
      uvicorn at startup.
    """
    path = _store_path()
    if not path.exists():
        return []

    try:
        with open(path, "r") as f:
            raw = json.load(f)
        jobs = [_deserialize(d) for d in raw]
    except Exception:
        try:
            backup = path.with_suffix(".corrupt")
            path.rename(backup)
            _log.warning(
                "jobs_store: %s was unreadable, renamed to %s — starting empty",
                path, backup,
            )
        except OSError:
            _log.warning(
                "jobs_store: %s was unreadable AND could not be renamed — starting empty",
                path,
            )
        return []

    now = time.time()
    timestamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    for job in jobs:
        if job.status in ("queued", "running", "canceling"):
            last_progress = ""
            if job.progress_stage or job.progress_pct > 0:
                last_progress = (
                    f" — last progress: {job.progress_pct:.0f}% {job.progress_stage}".rstrip()
                )
            job.error = (
                f"process restarted at {timestamp} before job finished "
                f"(likely OOM-kill or container restart){last_progress}"
            )
            job.status = "failed"
            job.finished_at = now
            job.cancel_requested = False
    return jobs
