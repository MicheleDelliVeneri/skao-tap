"""Unit tests for the tap-executor worker loop pieces on the fake pool."""

import contextlib
import datetime
import os

import pytest
from egernia_executor import worker

QUERY_SQL = "SELECT source_id, ra FROM ska.continuum_sources"


def _claim():
    claimed = worker.claim_job()
    assert claimed is not None
    return claimed


def _queued_job(fake_db, **overrides):
    return fake_db.add_job(
        phase="QUEUED",
        parameters={"QUERY": QUERY_SQL, "RESPONSEFORMAT": "csv"},
        query_sql=QUERY_SQL,
        **overrides,
    )


def test_claim_job_returns_oldest_queued(fake_db):
    assert worker.claim_job() is None
    older = _queued_job(fake_db, creation_time=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
    _queued_job(fake_db)
    claimed = _claim()
    assert claimed["job_id"] == older["job_id"]
    assert fake_db.jobs[older["job_id"]]["phase"] == "EXECUTING"
    assert claimed["start_time"] is not None
    assert claimed["worker_id"] == worker.WORKER_ID
    assert claimed["lease_expires"] > datetime.datetime.now(datetime.UTC)


def test_lease_renewal_requires_the_claim_owner(fake_db):
    job = _queued_job(fake_db)
    _claim()
    before = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)
    fake_db.jobs[job["job_id"]]["lease_expires"] = before

    assert worker._renew_lease(job["job_id"])
    assert fake_db.jobs[job["job_id"]]["lease_expires"] > before

    fake_db.jobs[job["job_id"]]["worker_id"] = "another-worker"
    expires = fake_db.jobs[job["job_id"]]["lease_expires"]
    assert not worker._renew_lease(job["job_id"])
    assert fake_db.jobs[job["job_id"]]["lease_expires"] == expires


def test_execute_job_writes_result_and_completes(fake_db, results_dir):
    job = _queued_job(fake_db)
    worker.execute_job(_claim())
    stored = fake_db.jobs[job["job_id"]]
    assert stored["phase"] == "COMPLETED"
    assert stored["result_mime"] == "text/csv"
    result_path = os.path.join(results_dir, job["job_id"], "result.csv")
    with open(result_path, "rb") as fh:
        content = fh.read()
    assert content.splitlines() == [b"source_id,ra", b"1,62.1", b"2,62.2"]
    assert stored["result_size"] == len(content)


def test_worker_that_lost_its_lease_cannot_replace_results(monkeypatch, fake_db, results_dir):
    job = _queued_job(fake_db)
    claimed = _claim()
    result_dir = os.path.join(results_dir, job["job_id"])
    result_path = os.path.join(result_dir, "result.csv")
    original_result_stream = worker.result_stream

    @contextlib.contextmanager
    def steal_lease_after_stream(*args, **kwargs):
        with original_result_stream(*args, **kwargs) as (chunks, limiter):

            def stolen_chunks():
                yield from chunks
                fake_db.jobs[job["job_id"]]["worker_id"] = "replacement-worker"
                with open(result_path, "wb") as fh:
                    fh.write(b"replacement")

            yield stolen_chunks(), limiter

    monkeypatch.setattr(worker, "result_stream", steal_lease_after_stream)
    worker.execute_job(claimed)

    assert fake_db.jobs[job["job_id"]]["phase"] == "EXECUTING"
    with open(result_path, "rb") as fh:
        assert fh.read() == b"replacement"
    assert not [name for name in os.listdir(result_dir) if name.endswith(".tmp")]


def test_execute_job_reads_tables_off_the_job_row(monkeypatch, fake_db, results_dir):
    """A job queued by the current API carries its table list, so the
    executor must not pay the per-job SQL parse the list used to cost."""

    def no_parse(sql):
        raise AssertionError("the executor re-parsed SQL despite a stored table list")

    monkeypatch.setattr(worker, "touched_tables", no_parse)
    job = _queued_job(fake_db, query_tables=["ska.continuum_sources"])
    worker.execute_job(_claim())
    assert fake_db.jobs[job["job_id"]]["phase"] == "COMPLETED"


def test_execute_job_falls_back_to_parsing_without_stored_tables(monkeypatch, fake_db, results_dir):
    """A job queued by an API that predates query_tables still executes,
    deriving the list from the SQL the way it always did."""
    parsed = []
    monkeypatch.setattr(worker, "touched_tables", lambda sql: parsed.append(sql) or set())
    job = _queued_job(fake_db)  # query_tables is NULL
    worker.execute_job(_claim())
    assert parsed == [QUERY_SQL]
    assert fake_db.jobs[job["job_id"]]["phase"] == "COMPLETED"


def test_execute_job_respects_maxrec(fake_db, results_dir):
    fake_db.result_rows = [(i, float(i)) for i in range(10)]
    job = _queued_job(fake_db)
    job["parameters"]["MAXREC"] = "3"
    worker.execute_job(_claim())
    result_path = os.path.join(results_dir, job["job_id"], "result.csv")
    with open(result_path, "rb") as fh:
        assert len(fh.read().splitlines()) == 4  # header + 3 rows


def test_execute_job_aborted_while_running_discards_result(fake_db, results_dir):
    job = _queued_job(fake_db)
    claimed = _claim()
    fake_db.jobs[job["job_id"]]["phase"] = "ABORTED"
    worker.execute_job(claimed)
    assert fake_db.jobs[job["job_id"]]["phase"] == "ABORTED"
    assert not os.path.isdir(os.path.join(results_dir, job["job_id"]))


def test_execute_job_failure_records_error(fake_db, results_dir):
    fake_db.result_error = RuntimeError("relation vanished")
    job = _queued_job(fake_db)
    worker.execute_job(_claim())
    stored = fake_db.jobs[job["job_id"]]
    assert stored["phase"] == "ERROR"
    assert stored["error_type"] == "fatal"
    assert "relation vanished" in stored["error_message"]
    assert not os.path.isdir(os.path.join(results_dir, job["job_id"]))


def test_expired_executor_lease_requeues_job(fake_db, results_dir):
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)
    job = fake_db.add_job(
        phase="EXECUTING",
        start_time=past,
        worker_id="dead-worker",
        lease_expires=past,
        backend_pid=123,
    )
    migrated_job = fake_db.add_job(phase="EXECUTING", lease_expires=None)
    result_dir = os.path.join(results_dir, job["job_id"])
    uploads_dir = os.path.join(result_dir, "uploads")
    os.makedirs(uploads_dir)
    partial = os.path.join(result_dir, "result.csv")
    with open(partial, "wb") as fh:
        fh.write(b"partial")

    assert worker.recover_orphaned_jobs() == 2

    stored = fake_db.jobs[job["job_id"]]
    assert stored["phase"] == "QUEUED"
    assert stored["start_time"] is None
    assert stored["worker_id"] is None
    assert stored["lease_expires"] is None
    assert stored["backend_pid"] is None
    assert fake_db.jobs[migrated_job["job_id"]]["phase"] == "QUEUED"
    assert not os.path.exists(partial)
    assert os.path.isdir(uploads_dir)


def test_cleanup_expired_removes_jobs_and_results(fake_db, results_dir):
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
    expired = fake_db.add_job(phase="COMPLETED", destruction=past)
    kept = fake_db.add_job(phase="COMPLETED")
    os.makedirs(os.path.join(results_dir, expired["job_id"]))
    worker.cleanup_expired()
    assert expired["job_id"] not in fake_db.jobs
    assert kept["job_id"] in fake_db.jobs
    assert not os.path.isdir(os.path.join(results_dir, expired["job_id"]))


def test_a_busy_executor_still_reports_the_queue(monkeypatch, fake_db, results_dir):
    """The loop used to `continue` straight past the queue metrics and the
    cleanup whenever a job was waiting — so a backlog stopped being reported
    exactly when it existed, and expired jobs stopped being destroyed."""
    calls = []
    slept = []

    def cleanup():
        calls.append("cleanup")
        if calls.count("cleanup") == 2:
            raise KeyboardInterrupt  # the only way out of a `while True`

    monkeypatch.setattr(worker, "QUEUE_METRICS_INTERVAL_S", 0)
    monkeypatch.setattr(worker, "CLEANUP_INTERVAL_S", 0)
    monkeypatch.setattr(worker, "claim_job", lambda: {"job_id": "always-a-job"})
    monkeypatch.setattr(worker, "execute_job", lambda job: None)
    monkeypatch.setattr(worker, "refresh_queue_metrics", lambda: calls.append("metrics"))
    monkeypatch.setattr(worker, "cleanup_expired", cleanup)
    monkeypatch.setattr(worker.bootstrap, "startup", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "start_http_server", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: slept.append(seconds))

    with pytest.raises(KeyboardInterrupt):
        worker.main()

    assert calls.count("metrics") >= 2
    assert slept == [], "a poll that found work must not throttle the next one"


def test_refresh_queue_metrics_reports_queue_depth(fake_db):
    from egernia_core.observability import REGISTRY

    _queued_job(fake_db)
    _queued_job(fake_db)
    worker.refresh_queue_metrics()
    assert REGISTRY.get_sample_value("tap_jobs", {"phase": "QUEUED"}) == 2.0
    oldest = REGISTRY.get_sample_value("tap_oldest_queued_job_seconds")
    assert oldest is not None and oldest >= 0.0


def test_refresh_queue_metrics_keeps_queued_series_alive_at_zero(fake_db):
    """The autoscaler reads max(tap_jobs{phase="QUEUED"}). max() over no
    series is empty, and an autoscaler reading emptiness cannot tell a
    drained queue from a broken exporter — so an empty queue must report 0
    rather than disappear."""
    from egernia_core.observability import REGISTRY

    fake_db.add_job(phase="COMPLETED")  # some other phase present, no QUEUED
    worker.refresh_queue_metrics()
    assert REGISTRY.get_sample_value("tap_jobs", {"phase": "QUEUED"}) == 0.0
    assert REGISTRY.get_sample_value("tap_oldest_queued_job_seconds") == 0.0
