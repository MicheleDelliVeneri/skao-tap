"""Unit tests for tap_api.queries.query parameter validation and sync execution."""

import pytest
from tap_api.queries.query import prepare_query, run_sync
from tapcore.config import settings
from tapcore.errors import UsageError

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
    assert any(s.startswith("SET LOCAL ROLE") for s in fake_db.statements)
