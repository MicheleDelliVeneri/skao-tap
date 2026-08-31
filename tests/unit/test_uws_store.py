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


def test_list_jobs_has_a_default_and_hard_bound(fake_db):
    for _ in range(uws.MAX_LIST_LIMIT + 1):
        fake_db.add_job()
    with pool().connection() as conn:
        assert len(uws.list_jobs(conn)) == uws.DEFAULT_LIST_LIMIT
        assert len(uws.list_jobs(conn, last=uws.MAX_LIST_LIMIT + 1)) == uws.MAX_LIST_LIMIT


def test_update_job(fake_db):
    job = fake_db.add_job()
    with pool().connection() as conn:
        uws.update_job(conn, job["job_id"], phase="QUEUED", query_sql="SELECT 1")
        assert fake_db.jobs[job["job_id"]]["phase"] == "QUEUED"
        assert not uws.update_job(
            conn, job["job_id"], expected_phases=("PENDING",), phase="EXECUTING"
        )
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


def test_signalling_a_backend_is_always_scoped_to_this_jobs_statement():
    """The `query LIKE` guard is what stops a signal reaching a PID that
    PostgreSQL has since handed to an unrelated backend. Both variants carry
    it, and both bind the pid and marker rather than interpolating them."""

    class Recording:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            return self

    conn = Recording()
    uws.signal_backend(conn, 4242, "%tap_job_abc%")
    uws.signal_backend(conn, 4242, "%tap_job_abc%", terminate=True)

    actions = [sql.split("(")[0].removeprefix("SELECT ") for sql, _ in conn.calls]
    assert actions == ["pg_cancel_backend", "pg_terminate_backend"]
    for sql, params in conn.calls:
        assert "FROM pg_stat_activity WHERE pid = %s AND query LIKE %s" in sql
        assert params == (4242, "%tap_job_abc%")
        assert "4242" not in sql  # bound, never interpolated
