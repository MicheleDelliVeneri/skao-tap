"""Unit tests for UWS 1.1 completeness: WAIT blocking, AFTER filtering,
and real ABORT via pg_cancel_backend (roadmap package 3)."""

import datetime

from tap_api import uws_api

QUERY = "SELECT source_id, ra FROM ska.continuum_sources"


def _flip_phase_on_sleep(monkeypatch, fake_db, job_id, to_phase):
    """Patch the wait loop's sleep to flip the job phase — deterministic
    phase-change wakeups without real time passing."""

    async def flipping_sleep(seconds):
        fake_db.jobs[job_id]["phase"] = to_phase

    monkeypatch.setattr(uws_api.asyncio, "sleep", flipping_sleep)


def test_wait_validates_parameters(client, fake_db):
    job = fake_db.add_job(phase="EXECUTING")
    assert client.get(f"/tap/async/{job['job_id']}", params={"WAIT": "soon"}).status_code == 400
    assert client.get(f"/tap/async/{job['job_id']}", params={"WAIT": "-2"}).status_code == 400
    assert (
        client.get(
            f"/tap/async/{job['job_id']}/phase", params={"WAIT": "5", "PHASE": "NAPPING"}
        ).status_code
        == 400
    )


def test_wait_returns_immediately_for_final_phase(client, fake_db):
    job = fake_db.add_job(phase="COMPLETED")
    response = client.get(f"/tap/async/{job['job_id']}/phase", params={"WAIT": "30"})
    assert response.text == "COMPLETED"


def test_wait_returns_immediately_on_phase_mismatch(client, fake_db, monkeypatch):
    async def must_not_sleep(seconds):
        raise AssertionError("WAIT blocked despite PHASE mismatch")

    monkeypatch.setattr(uws_api.asyncio, "sleep", must_not_sleep)
    job = fake_db.add_job(phase="EXECUTING")
    response = client.get(
        f"/tap/async/{job['job_id']}/phase", params={"WAIT": "30", "PHASE": "QUEUED"}
    )
    assert response.text == "EXECUTING"


def test_wait_blocks_until_phase_changes(client, fake_db, monkeypatch):
    job = fake_db.add_job(phase="EXECUTING")
    _flip_phase_on_sleep(monkeypatch, fake_db, job["job_id"], "COMPLETED")
    response = client.get(f"/tap/async/{job['job_id']}/phase", params={"WAIT": "30"})
    assert response.text == "COMPLETED"
    summary = fake_db.add_job(phase="QUEUED")
    _flip_phase_on_sleep(monkeypatch, fake_db, summary["job_id"], "EXECUTING")
    response = client.get(f"/tap/async/{summary['job_id']}", params={"WAIT": "-1"})
    assert "<uws:phase>EXECUTING</uws:phase>" in response.text


def test_wait_expires_with_unchanged_phase(client, fake_db, monkeypatch):
    ticks = iter(range(0, 100, 2))  # fake clock: 2 "seconds" per call

    async def instant(seconds):
        pass

    monkeypatch.setattr(uws_api.asyncio, "sleep", instant)
    monkeypatch.setattr(uws_api.time, "monotonic", lambda: float(next(ticks)))
    job = fake_db.add_job(phase="EXECUTING")
    response = client.get(f"/tap/async/{job['job_id']}/phase", params={"WAIT": "3"})
    assert response.text == "EXECUTING"


def test_json_wait_blocks_until_phase_changes(client, fake_db, monkeypatch):
    job = fake_db.add_job(phase="EXECUTING")
    _flip_phase_on_sleep(monkeypatch, fake_db, job["job_id"], "ERROR")

    import tap_api.json_api as json_api

    monkeypatch.setattr(json_api.asyncio, "sleep", uws_api.asyncio.sleep, raising=False)
    response = client.get(f"/api/v1/jobs/{job['job_id']}", params={"wait": 30})
    assert response.json()["phase"] == "ERROR"


def test_after_filters_job_list(client, fake_db):
    old = fake_db.add_job(creation_time=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC))
    new = fake_db.add_job(creation_time=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC))
    listing = client.get("/tap/async", params={"AFTER": "2026-06-01T00:00:00Z"})
    assert new["job_id"] in listing.text
    assert old["job_id"] not in listing.text
    assert client.get("/tap/async", params={"AFTER": "yesterday"}).status_code == 400

    body = client.get("/api/v1/jobs", params={"after": "2026-06-01T00:00:00Z"}).json()
    assert [j["job_id"] for j in body["jobs"]] == [new["job_id"]]
    assert client.get("/api/v1/jobs", params={"after": "nope"}).status_code == 400


def test_abort_cancels_running_backend(client, fake_db):
    job = fake_db.add_job(phase="EXECUTING", backend_pid=4242)
    client.post(
        f"/tap/async/{job['job_id']}/phase", data={"PHASE": "ABORT"}, follow_redirects=False
    )
    assert fake_db.jobs[job["job_id"]]["phase"] == "ABORTED"
    assert fake_db.cancelled == [4242]


def test_json_abort_cancels_running_backend(client, fake_db):
    job = fake_db.add_job(phase="EXECUTING", backend_pid=777)
    response = client.post(f"/api/v1/jobs/{job['job_id']}/phase", json={"phase": "ABORT"})
    assert response.json()["phase"] == "ABORTED"
    assert fake_db.cancelled == [777]


def test_abort_of_final_job_does_not_cancel(client, fake_db):
    job = fake_db.add_job(phase="COMPLETED", backend_pid=4242)
    client.post(
        f"/tap/async/{job['job_id']}/phase", data={"PHASE": "ABORT"}, follow_redirects=False
    )
    assert fake_db.jobs[job["job_id"]]["phase"] == "COMPLETED"
    assert fake_db.cancelled == []


def test_executor_registers_and_clears_backend_pid(fake_db, results_dir):
    from tap_executor import worker

    job = fake_db.add_job(phase="QUEUED", parameters={"QUERY": QUERY}, query_sql=QUERY)
    worker.execute_job(worker.claim_job())
    assert any(s.startswith("SELECT pg_backend_pid()") for s in fake_db.statements)
    stored = fake_db.jobs[job["job_id"]]
    assert stored["phase"] == "COMPLETED"
    assert stored["backend_pid"] is None


def test_executor_treats_cancellation_of_aborted_job_as_abort(fake_db, results_dir):
    from tap_executor import worker

    fake_db.result_error = RuntimeError("canceling statement due to user request")
    job = fake_db.add_job(phase="QUEUED", parameters={"QUERY": QUERY}, query_sql=QUERY)
    claimed = worker.claim_job()
    fake_db.jobs[job["job_id"]]["phase"] = "ABORTED"  # ABORT raced the execution
    worker.execute_job(claimed)
    stored = fake_db.jobs[job["job_id"]]
    assert stored["phase"] == "ABORTED"
    assert stored["error_message"] is None


def test_abort_skips_cancel_when_pid_already_cleared(client, fake_db):
    """A finished execution clears backend_pid; an ABORT racing it must not
    cancel the PID from its stale read — that connection is pooled again."""
    job = fake_db.add_job(phase="EXECUTING", backend_pid=4242)
    stale = dict(job)  # what the API handler read before the executor finished
    fake_db.jobs[job["job_id"]]["backend_pid"] = None
    from tapcore import uws
    from tapcore.db import pool

    with pool().connection() as conn:
        uws.abort_job(conn, stale)
    assert fake_db.jobs[job["job_id"]]["phase"] == "ABORTED"
    assert fake_db.cancelled == []
