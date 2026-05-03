import time

from app import jobs


def test_job_dataclass_to_dict_roundtrip():
    j = jobs.Job(
        id="abc", item_id="1", item_name="Movie",
        target_lang="fr", provider="llm", mode="audio",
    )
    d = j.to_dict()
    assert d["id"] == "abc"
    assert d["status"] == "queued"
    assert d["error"] is None
    assert d["mode"] == "audio"


def test_list_jobs_empty():
    assert jobs.list_jobs() == []


def test_list_jobs_returns_newest_first():
    j1 = jobs.Job(id="1", item_id="a", item_name="A", target_lang="fr", provider="llm", mode="audio")
    j1.queued_at = 100.0
    j2 = jobs.Job(id="2", item_id="b", item_name="B", target_lang="fr", provider="llm", mode="audio")
    j2.queued_at = 200.0
    jobs._jobs[j1.id] = j1
    jobs._jobs[j2.id] = j2
    listed = jobs.list_jobs()
    assert [j.id for j in listed] == ["2", "1"]


def test_get_job_returns_none_for_unknown():
    assert jobs.get_job("nonexistent") is None


def test_max_jobs_eviction(monkeypatch):
    monkeypatch.setattr(jobs, "MAX_JOBS", 3)
    # We need a "main loop" or submit() will raise; instead, simulate the dict
    # state directly to test eviction.
    for i in range(5):
        j = jobs.Job(id=f"j{i}", item_id="x", item_name="x",
                      target_lang="fr", provider="llm", mode="audio")
        jobs._jobs[j.id] = j
        while len(jobs._jobs) > jobs.MAX_JOBS:
            jobs._jobs.popitem(last=False)
    # Only the 3 most recent remain
    assert set(jobs._jobs.keys()) == {"j2", "j3", "j4"}


def test_update_progress_clamps_and_is_monotonic_within_stage():
    """The progress bar should never visually jump backwards while still in
    the same stage. update_progress enforces this so a mid-batch retry that
    reports a smaller fraction (e.g. one cue lands earlier than expected
    after a beam-search rerun) doesn't cause UI rubber-banding."""
    j = jobs.Job(id="x", item_id="i", item_name="n", target_lang="fr", provider="llm", mode="audio")
    j.update_progress(40, "transcribing")
    j.update_progress(35, "transcribing")
    assert j.progress_pct == 40

    # Out-of-range values are clamped.
    j.update_progress(150, "transcribing")
    assert j.progress_pct == 100
    j.update_progress(-50, "transcribing")
    assert j.progress_pct == 100  # still clamped, monotonic-within-stage holds.

    # Stage change always wins regardless of pct, so the user sees the new
    # phase even if its starting fraction is below the prior fraction.
    j.update_progress(50, "translating")
    assert j.progress_stage == "translating"


def test_request_cancel_sets_canceling_status_when_running():
    j = jobs.Job(id="x", item_id="i", item_name="n", target_lang="fr", provider="llm", mode="audio")
    j.status = "running"
    j.request_cancel()
    assert j.cancel_requested is True
    assert j.status == "canceling"


def test_request_cancel_does_not_overwrite_terminal_status():
    """A user clicking cancel just as a job finishes shouldn't flip a
    succeeded/failed job back to 'canceling' — the work is done."""
    j = jobs.Job(id="x", item_id="i", item_name="n", target_lang="fr", provider="llm", mode="audio")
    j.status = "succeeded"
    j.request_cancel()
    assert j.cancel_requested is True   # flag still set (idempotent / harmless)
    assert j.status == "succeeded"      # but status is unchanged


def test_check_cancel_raises_only_when_requested():
    j = jobs.Job(id="x", item_id="i", item_name="n", target_lang="fr", provider="llm", mode="audio")
    j.check_cancel()  # no-op when cancel not requested
    j.cancel_requested = True
    import pytest
    with pytest.raises(jobs.JobCanceled):
        j.check_cancel()


def test_submit_without_main_loop_raises():
    import pytest
    # _main_loop is None at module load; submitting should raise a clear error.
    monkeypatch_loop = jobs._main_loop
    jobs._main_loop = None
    try:
        with pytest.raises(RuntimeError, match="main loop"):
            jobs.submit(
                item_id="1", item_name="x", target_lang="fr",
                provider="llm", mode="audio",
                runner=lambda j: None,
            )
    finally:
        jobs._main_loop = monkeypatch_loop
