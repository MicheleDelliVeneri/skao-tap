"""Unit tests for the egernia_core.uws persistence helpers on the fake pool."""

import pytest
from egernia_core import db, uws
from egernia_core.errors import NotFoundError

pool = db.pool


def test_create_and_get_job(fake_db):
    with pool().connection() as conn:
        job = uws.create_job(conn, {"QUERY": "SELECT 1", "RUNID": "r1"}, owner_id="alice")
        assert job["phase"] == "PENDING"
        assert job["run_id"] == "r1"
        assert job["owner_id"] == "alice"
        assert job["parameters"] == {"QUERY": "SELECT 1", "RUNID": "r1"}
        assert uws.get_job(conn, job["job_id"])["job_id"] == job["job_id"]


def test_get_job_missing_raises(fake_db):
    with pool().connection() as conn, pytest.raises(NotFoundError):
        uws.get_job(conn, "0123456789abcdef")


def test_list_jobs_filters_and_limits(fake_db):
    fake_db.add_job(phase="ARCHIVED")
    fake_db.add_job(phase="EXECUTING")
    fake_db.add_job(phase="COMPLETED")
    with pool().connection() as conn:
        assert len(uws.list_jobs(conn)) == 2  # ARCHIVED hidden by default
        assert [j["phase"] for j in uws.list_jobs(conn, ["ARCHIVED"])] == ["ARCHIVED"]
        assert len(uws.list_jobs(conn, None, 1)) == 1


def test_update_job(fake_db):
    job = fake_db.add_job()
    with pool().connection() as conn:
        uws.update_job(conn, job["job_id"], phase="QUEUED", query_sql="SELECT 1")
        assert fake_db.jobs[job["job_id"]]["phase"] == "QUEUED"
        uws.update_job(conn, job["job_id"])  # no fields: no-op
        with pytest.raises(NotFoundError):
            uws.update_job(conn, "0123456789abcdef", phase="QUEUED")


def test_delete_job(fake_db):
    job = fake_db.add_job()
    with pool().connection() as conn:
        uws.delete_job(conn, job["job_id"])
        assert fake_db.jobs == {}
        with pytest.raises(NotFoundError):
            uws.delete_job(conn, job["job_id"])


def test_pool_is_cached_and_closable(monkeypatch):
    created = []

    class StubPool:
        def __init__(self, url, **kwargs):
            created.append(url)
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(db, "ConnectionPool", StubPool)
    monkeypatch.setattr(db, "_pool", None)
    first = db.pool()
    assert db.pool() is first
    assert len(created) == 1
    db.close_pool()
    assert first.closed
    assert db._pool is None
    db.close_pool()  # idempotent when already closed
