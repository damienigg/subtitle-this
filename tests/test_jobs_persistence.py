"""Tests for the on-disk jobs persistence layer (app/jobs_store.py).

The load-bearing behaviors:

- Atomic write: jobs.json is replaced via .tmp + os.replace so a kill
  mid-save can never corrupt the file.
- Orphan recovery: jobs persisted as running / queued / canceling come
  back as failed with a descriptive error after a simulated restart.
  This is what the user SEES in the dashboard after an OOM-kill.
- Throttled progress writes: rapid update_progress calls collapse into
  at most one disk write per PROGRESS_SAVE_INTERVAL_S window.
- Corrupted file: parse failures rename to .corrupt and start empty
  rather than crashing uvicorn at startup.
"""
import json
import time
from collections import OrderedDict
from pathlib import Path

import pytest

from app import jobs, jobs_store
from app.jobs import Job


@pytest.fixture(autouse=True)
def _isolate_jobs_state(tmp_path, monkeypatch):
    """Each test gets a fresh in-memory queue + on-disk file. Without
    this, tests pollute each other because jobs._jobs is module-level."""
    from app.config import settings as runtime_settings
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(
        runtime_settings, "_overrides",
        {**runtime_settings._overrides, "cache_dir": str(cache_dir)},
    )
    # Reset in-memory queue and throttle state for each test.
    monkeypatch.setattr(jobs, "_jobs", OrderedDict())
    monkeypatch.setattr(jobs_store, "_last_progress_save", {})
    yield


def _make_job(**overrides) -> Job:
    base = dict(
        id="abc123",
        item_id="item-1",
        item_name="movie.mkv",
        target_lang="fr",
        provider="nllb",
    )
    base.update(overrides)
    return Job(**base)


# ── Save / load round-trip ────────────────────────────────────────────────


def test_save_and_load_roundtrip_preserves_terminal_jobs(tmp_path):
    """A job in a terminal state (succeeded/failed/canceled) reloads with
    the exact same fields. No orphan rewrite."""
    j = _make_job(
        status="succeeded", progress_pct=100.0, progress_stage="done",
        cue_count=42, output_path="/data/out.vtt",
        started_at=1234.0, finished_at=1300.0,
    )
    jobs_store.save_jobs([j])
    loaded = jobs_store.load_jobs()
    assert len(loaded) == 1
    assert loaded[0].status == "succeeded"
    assert loaded[0].cue_count == 42
    assert loaded[0].output_path == "/data/out.vtt"
    assert loaded[0].finished_at == 1300.0


def test_load_returns_empty_when_no_file_exists():
    """First boot — no jobs.json yet. Don't crash, return []."""
    assert jobs_store.load_jobs() == []


# ── Orphan recovery ───────────────────────────────────────────────────────


def test_running_job_becomes_failed_with_progress_info_on_load():
    """The critical case: an OOM-kill cuts uvicorn while a job was at
    78% transcribing. After Docker restarts the container, the user
    must see what happened in the dashboard."""
    j = _make_job(
        status="running", progress_pct=78.0, progress_stage="transcribing",
        started_at=1000.0,
    )
    jobs_store.save_jobs([j])
    loaded = jobs_store.load_jobs()
    assert len(loaded) == 1
    rec = loaded[0]
    assert rec.status == "failed"
    assert "process restarted" in rec.error
    assert "78%" in rec.error
    assert "transcribing" in rec.error
    assert rec.finished_at is not None


def test_queued_job_becomes_failed_on_load():
    """A job that never even started running still gets surfaced as
    failed (rather than vanishing) so the user knows their click was
    registered before the process died."""
    j = _make_job(status="queued")
    jobs_store.save_jobs([j])
    loaded = jobs_store.load_jobs()
    assert loaded[0].status == "failed"
    assert "process restarted" in loaded[0].error


def test_canceling_job_becomes_failed_on_load():
    """User clicked cancel but the pipeline never reached its next
    check_cancel() before the process died. Surface as failed too —
    we don't know if the cancel would have completed normally."""
    j = _make_job(status="canceling", progress_pct=50.0)
    jobs_store.save_jobs([j])
    loaded = jobs_store.load_jobs()
    assert loaded[0].status == "failed"


def test_terminal_states_are_left_alone_on_load():
    """succeeded / failed / canceled jobs are not touched — they already
    have their real outcome on disk."""
    for term in ("succeeded", "failed", "canceled"):
        j = _make_job(id=f"id-{term}", status=term,
                      error="original" if term == "failed" else None)
        jobs_store.save_jobs([j])
        loaded = jobs_store.load_jobs()
        assert loaded[0].status == term
        if term == "failed":
            assert loaded[0].error == "original"


# ── Atomic writes ─────────────────────────────────────────────────────────


def test_save_uses_atomic_replace(tmp_path):
    """The tmp sidecar must never linger after a successful save — its
    presence post-write would mean a half-finished state."""
    j = _make_job(status="succeeded")
    jobs_store.save_jobs([j])
    path = jobs_store._store_path()
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()


def test_save_failure_does_not_destroy_existing_file(tmp_path, monkeypatch):
    """If a save throws mid-write, the previous good file must remain
    intact. We can't easily corrupt os.replace without root, but we CAN
    verify the swallowed-exception behavior: a failing dump leaves the
    in-memory state OK and the existing file untouched."""
    j1 = _make_job(id="old", status="succeeded")
    jobs_store.save_jobs([j1])
    path = jobs_store._store_path()
    snapshot_before = path.read_bytes()

    # Simulate a JSON serialization failure mid-save. As of 0.8.3 the
    # store routes through app.util.atomic_write, which calls
    # json.dumps once before the tmp+replace dance — patching
    # ``json.dumps`` here makes the save fail before any on-disk
    # mutation, exercising the swallowed-exception path.
    def boom(*a, **kw):
        raise RuntimeError("simulated disk fail")
    monkeypatch.setattr(jobs_store.json, "dumps", boom)

    jobs_store.save_jobs([_make_job(id="new", status="running")])

    # Previous good file is still there, unchanged.
    assert path.read_bytes() == snapshot_before


# ── Corrupted file recovery ───────────────────────────────────────────────


def test_corrupted_jobs_file_is_backed_up_and_starts_empty(tmp_path):
    """If jobs.json is unreadable JSON, we rename to .corrupt and start
    fresh — rather than crashing uvicorn at startup over a stale file."""
    path = jobs_store._store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json {{{")

    result = jobs_store.load_jobs()
    assert result == []
    assert path.with_suffix(".corrupt").exists()
    assert not path.exists()   # original moved out of the way


# ── Throttled writes ──────────────────────────────────────────────────────


def test_throttled_save_collapses_rapid_calls(monkeypatch):
    """Progress callbacks fire many times per second; the throttled
    variant must not flood the disk. First call writes; subsequent
    calls within PROGRESS_SAVE_INTERVAL_S are no-ops."""
    saves: list = []
    monkeypatch.setattr(jobs_store, "save_jobs",
                        lambda jobs_list: saves.append(len(jobs_list)))

    j = _make_job(status="running")
    for _ in range(50):
        jobs_store.save_jobs_throttled([j], "abc123")

    assert len(saves) == 1   # only the first one fired


def test_throttled_save_fires_again_after_interval(monkeypatch):
    """Once PROGRESS_SAVE_INTERVAL_S has elapsed, the next call must
    write again — the throttle is a rate-limiter, not a one-shot."""
    saves: list = []
    monkeypatch.setattr(jobs_store, "save_jobs",
                        lambda jobs_list: saves.append(time.monotonic()))

    j = _make_job(status="running")
    jobs_store.save_jobs_throttled([j], "abc123")
    # Fake the clock so the throttle window has elapsed.
    base = jobs_store._last_progress_save["abc123"]
    jobs_store._last_progress_save["abc123"] = (
        base - jobs_store.PROGRESS_SAVE_INTERVAL_S - 1
    )
    jobs_store.save_jobs_throttled([j], "abc123")

    assert len(saves) == 2


# ── jobs.py integration: state transitions reach the disk ─────────────────


def test_submit_persists_queued_job_to_disk():
    """jobs.submit() must write to disk before returning — otherwise a
    crash between submit and the asyncio scheduler picking up the job
    leaves no trace."""
    import asyncio

    # set_main_loop with a dummy loop so submit doesn't raise.
    async def fake_run(j): ...
    loop = asyncio.new_event_loop()
    try:
        jobs.set_main_loop(loop)
        job = jobs.submit(
            item_id="x", item_name="m.mkv", target_lang="fr",
            provider="nllb", runner=fake_run,
        )
        path = jobs_store._store_path()
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["id"] == job.id
        assert data[0]["status"] == "queued"
    finally:
        loop.close()


def test_load_persisted_brings_orphans_into_in_memory_dict():
    """jobs.load_persisted() is the startup hook called from lifespan.
    It must populate _jobs AND rewrite any orphans as failed."""
    j = _make_job(id="orphan", status="running", progress_pct=42.0,
                  progress_stage="translating")
    jobs_store.save_jobs([j])

    jobs.load_persisted()

    assert "orphan" in jobs._jobs
    recovered = jobs._jobs["orphan"]
    assert recovered.status == "failed"
    assert "42%" in recovered.error
    assert "translating" in recovered.error


def test_load_persisted_is_noop_when_no_file():
    """First boot — no jobs.json yet. load_persisted should not crash
    and the in-memory dict must remain empty."""
    jobs.load_persisted()
    assert len(jobs._jobs) == 0


# ── clear_finished_jobs ───────────────────────────────────────────────────


def test_clear_finished_drops_terminal_and_persists():
    """The dashboard "Clear finished jobs" button removes succeeded /
    failed / canceled entries and persists the trimmed list so a process
    restart doesn't bring them back."""
    survivors = [
        _make_job(id="r-running", status="running"),
        _make_job(id="q-queued", status="queued"),
        _make_job(id="c-canceling", status="canceling"),
    ]
    to_drop = [
        _make_job(id="s-ok", status="succeeded"),
        _make_job(id="f-bad", status="failed", error="OOM"),
        _make_job(id="x-canceled", status="canceled"),
    ]
    for j in survivors + to_drop:
        jobs._jobs[j.id] = j

    n = jobs.clear_finished_jobs()

    assert n == 3
    assert set(jobs._jobs.keys()) == {"r-running", "q-queued", "c-canceling"}
    # Disk reflects the trimmed list — the next process restart must not
    # resurrect the cleared entries.
    persisted = jobs_store.load_jobs()
    assert {j.id for j in persisted} == {"r-running", "q-queued", "c-canceling"}


def test_clear_finished_returns_zero_when_nothing_to_drop():
    """No terminal jobs present — clear is a clean no-op, no disk write
    needed (the function only persists when something actually changed)."""
    jobs._jobs[_make_job(id="r1", status="running").id] = _make_job(
        id="r1", status="running",
    )
    jobs._jobs[_make_job(id="q1", status="queued").id] = _make_job(
        id="q1", status="queued",
    )

    n = jobs.clear_finished_jobs()

    assert n == 0
    assert set(jobs._jobs.keys()) == {"r1", "q1"}


def test_clear_finished_leaves_canceling_jobs_alone():
    """A canceling job has a runner coroutine still ticking — deleting it
    would orphan that coroutine. The user must wait for it to land in
    'canceled' before clearing it."""
    jobs._jobs["x"] = _make_job(id="x", status="canceling")

    n = jobs.clear_finished_jobs()

    assert n == 0
    assert "x" in jobs._jobs
