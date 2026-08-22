"""Unit tests for the tap-executor worker loop pieces on the fake pool."""

import datetime
import os

from tap_executor import worker

QUERY_SQL = "SELECT source_id, ra FROM ska.continuum_sources"


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
    claimed = worker.claim_job()
    assert claimed["job_id"] == older["job_id"]
    assert fake_db.jobs[older["job_id"]]["phase"] == "EXECUTING"
    assert claimed["start_time"] is not None


def test_execute_job_writes_result_and_completes(fake_db, results_dir):
    job = _queued_job(fake_db)
    worker.execute_job(worker.claim_job())
    stored = fake_db.jobs[job["job_id"]]
    assert stored["phase"] == "COMPLETED"
    assert stored["result_mime"] == "text/csv"
    result_path = os.path.join(results_dir, job["job_id"], "result.csv")
    with open(result_path, "rb") as fh:
        content = fh.read()
    assert content.splitlines() == [b"source_id,ra", b"1,62.1", b"2,62.2"]
    assert stored["result_size"] == len(content)


def test_execute_job_respects_maxrec(fake_db, results_dir):
    fake_db.result_rows = [(i, float(i)) for i in range(10)]
    job = _queued_job(fake_db)
    job["parameters"]["MAXREC"] = "3"
    worker.execute_job(worker.claim_job())
    result_path = os.path.join(results_dir, job["job_id"], "result.csv")
    with open(result_path, "rb") as fh:
        assert len(fh.read().splitlines()) == 4  # header + 3 rows


def test_execute_job_aborted_while_running_discards_result(fake_db, results_dir):
    job = _queued_job(fake_db)
    claimed = worker.claim_job()
    fake_db.jobs[job["job_id"]]["phase"] = "ABORTED"
    worker.execute_job(claimed)
    assert fake_db.jobs[job["job_id"]]["phase"] == "ABORTED"
    assert not os.path.isdir(os.path.join(results_dir, job["job_id"]))


def test_execute_job_failure_records_error(fake_db, results_dir):
    fake_db.result_error = RuntimeError("relation vanished")
    job = _queued_job(fake_db)
    worker.execute_job(worker.claim_job())
    stored = fake_db.jobs[job["job_id"]]
    assert stored["phase"] == "ERROR"
    assert stored["error_type"] == "fatal"
    assert "relation vanished" in stored["error_message"]
    assert not os.path.isdir(os.path.join(results_dir, job["job_id"]))


def test_cleanup_expired_removes_jobs_and_results(fake_db, results_dir):
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
    expired = fake_db.add_job(phase="COMPLETED", destruction=past)
    kept = fake_db.add_job(phase="COMPLETED")
    os.makedirs(os.path.join(results_dir, expired["job_id"]))
    worker.cleanup_expired()
    assert expired["job_id"] not in fake_db.jobs
    assert kept["job_id"] in fake_db.jobs
    assert not os.path.isdir(os.path.join(results_dir, expired["job_id"]))
