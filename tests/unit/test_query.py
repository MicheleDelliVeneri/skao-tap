"""Unit tests for egernia_api.queries.query parameter validation and sync execution."""

import asyncio
import contextlib
import threading
from dataclasses import replace
from typing import Any, cast

import pytest
from egernia_api.queries import query
from egernia_api.queries.query import prepare_query, run_sync
from egernia_core.config import settings
from egernia_core.errors import UsageError
from starlette.requests import ClientDisconnect

QUERY = "SELECT source_id, ra FROM ska.continuum_sources"


def test_prepare_query_defaults(fake_db):
    prepared = prepare_query({"QUERY": QUERY})
    assert "ska.continuum_sources" in prepared["sql"]
    assert prepared["tables"] == {"ska.continuum_sources"}
    assert prepared["maxrec"] == settings.default_maxrec
    assert prepared["fmt_key"] == "votable"
    assert prepared["mime"] == "application/x-votable+xml"
    assert prepared["ext"] == "vot"


def test_prepare_query_maxrec_and_format(fake_db):
    prepared = prepare_query({"QUERY": QUERY, "MAXREC": "5", "RESPONSEFORMAT": "json"})
    assert prepared["maxrec"] == 5
    assert prepared["fmt_key"] == "json"


def test_prepare_query_maxrec_clamped_to_hard_limit(fake_db):
    prepared = prepare_query({"QUERY": QUERY, "MAXREC": str(settings.hard_maxrec + 1)})
    assert prepared["maxrec"] == settings.hard_maxrec


def test_prepare_query_format_falls_back_to_legacy_param(fake_db):
    prepared = prepare_query({"QUERY": QUERY, "FORMAT": "csv"})
    assert prepared["fmt_key"] == "csv"


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"QUERY": QUERY, "REQUEST": "getAvailability"}, "REQUEST=getAvailability"),
        ({"QUERY": QUERY, "UPLOAD": "notapair"}, "UPLOAD"),
        ({"QUERY": QUERY, "MAXREC": "many"}, "MAXREC=many"),
        ({"QUERY": QUERY, "MAXREC": "-1"}, "MAXREC must be >= 0"),
        ({"QUERY": QUERY, "RESPONSEFORMAT": "xml3"}, "RESPONSEFORMAT=xml3"),
        ({"QUERY": "SELECT a FROM private.hidden"}, "not published"),
    ],
)
def test_prepare_query_rejects_bad_parameters(fake_db, params, match):
    with pytest.raises(UsageError, match=match):
        prepare_query(params)


def test_run_sync_streams_rows(fake_db):
    prepared = prepare_query({"QUERY": QUERY, "RESPONSEFORMAT": "csv"})
    chunks, mime = run_sync(prepared)
    assert mime == "text/csv"
    body = b"".join(chunks).decode()
    assert body.splitlines() == ["source_id,ra", "1,62.1", "2,62.2"]


def test_run_sync_releases_connection_before_body_is_read(fake_db, monkeypatch):
    prepared = prepare_query({"QUERY": QUERY, "RESPONSEFORMAT": "csv"})
    real_connection = query.db_connection
    active = 0

    @contextlib.contextmanager
    def tracked_connection():
        nonlocal active
        with real_connection() as conn:
            active += 1
            try:
                yield conn
            finally:
                active -= 1

    monkeypatch.setattr(query, "db_connection", tracked_connection)
    chunks, _ = run_sync(prepared)
    assert active == 0
    assert next(chunks).startswith(b"source_id,ra")
    cast(Any, chunks).close()  # client disconnected before consuming the body


def test_run_sync_rolls_to_disk_and_closes_spool(fake_db, monkeypatch):
    fake_db.result_rows = [("x" * 1024, float(i)) for i in range(2048)]
    opened = []
    real_spool = query.tempfile.SpooledTemporaryFile

    def tracked_spool(*args, **kwargs):
        spool = real_spool(*args, **kwargs)
        opened.append(spool)
        return spool

    monkeypatch.setattr(query.tempfile, "SpooledTemporaryFile", tracked_spool)
    chunks, _ = run_sync(prepare_query({"QUERY": QUERY, "RESPONSEFORMAT": "csv"}))
    assert opened[0]._rolled  # proves the stdlib spool crossed to disk
    b"".join(chunks)
    assert opened[0].closed


def test_run_sync_bounds_and_cleans_failed_spools(fake_db, monkeypatch):
    monkeypatch.setattr(query, "settings", replace(settings, sync_max_bytes=1))
    with pytest.raises(UsageError, match="use async"):
        run_sync(prepare_query({"QUERY": QUERY, "RESPONSEFORMAT": "csv"}))

    class BrokenSpool:
        closed = False

        def write(self, chunk):
            raise OSError("disk full")

        def close(self):
            self.closed = True

    broken = BrokenSpool()
    monkeypatch.setattr(query, "settings", replace(settings, sync_max_bytes=1024))
    monkeypatch.setattr(query.tempfile, "SpooledTemporaryFile", lambda **kwargs: broken)
    with pytest.raises(OSError, match="disk full"):
        run_sync(prepare_query({"QUERY": QUERY, "RESPONSEFORMAT": "csv"}))
    assert broken.closed


def test_disconnect_cancels_spool_before_response(fake_db, monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ClientDisconnect):
        cast(Any, run_sync)(prepare_query({"QUERY": QUERY}), cancelled=cancelled)

    class Disconnected:
        async def is_disconnected(self):
            return True

    def wait_for_cancel(prepared, uploads, cancelled):
        assert cancelled.wait(1)
        raise ClientDisconnect

    monkeypatch.setattr(query, "run_sync", wait_for_cancel)
    with pytest.raises(ClientDisconnect):
        asyncio.run(cast(Any, query).run_sync_for_request(Disconnected(), {"mime": "text/csv"}))


def test_run_sync_reports_overflow(fake_db):
    fake_db.result_rows = [(i, float(i)) for i in range(5)]
    prepared = prepare_query({"QUERY": QUERY, "MAXREC": "2", "RESPONSEFORMAT": "json"})
    chunks, _ = run_sync(prepared)
    body = b"".join(chunks).decode()
    assert '"status": "OVERFLOW"' in body


def test_run_sync_sets_timeout_and_role(fake_db):
    prepared = prepare_query({"QUERY": QUERY})
    chunks, _ = run_sync(prepared)
    b"".join(chunks)
    assert any("set_config" in s for s in fake_db.statements)
    assert any("set_config('role'" in s for s in fake_db.statements)
