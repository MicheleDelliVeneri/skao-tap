"""VOSI endpoints: availability, capabilities (TAPRegExt), tables (VODataService)."""

import contextlib
from xml.etree import ElementTree as ET

import httpx
import pytest
import pyvo

pytestmark = pytest.mark.component

NS = {
    "vosi-avail": "http://www.ivoa.net/xml/VOSIAvailability/v1.0",
    "vosi-caps": "http://www.ivoa.net/xml/VOSICapabilities/v1.0",
    "vosi-tables": "http://www.ivoa.net/xml/VOSITables/v1.0",
    "vod": "http://www.ivoa.net/xml/VODataService/v1.1",
}


def test_availability(tap_service):
    response = httpx.get(f"{tap_service}/availability")
    assert response.status_code == 200
    root = ET.fromstring(response.text)
    assert root.find("vosi-avail:available", NS).text == "true"


def test_capabilities_declare_tap_and_vosi(tap_service):
    root = ET.fromstring(httpx.get(f"{tap_service}/capabilities").text)
    standard_ids = {c.get("standardID") for c in root.findall("capability")}
    assert "ivo://ivoa.net/std/TAP" in standard_ids
    assert "ivo://ivoa.net/std/VOSI#capabilities" in standard_ids
    assert "ivo://ivoa.net/std/VOSI#availability" in standard_ids
    assert "ivo://ivoa.net/std/VOSI#tables" in standard_ids

    tap_cap = next(
        c for c in root.findall("capability") if c.get("standardID") == "ivo://ivoa.net/std/TAP"
    )
    languages = {lang.findtext("name") for lang in tap_cap.findall("language")}
    assert "ADQL" in languages
    mimes = {of.findtext("mime") for of in tap_cap.findall("outputFormat")}
    assert {"application/x-votable+xml", "text/csv", "text/tab-separated-values"} <= mimes


def test_capabilities_parse_with_pyvo(tap_service):
    svc = pyvo.dal.TAPService(tap_service)
    ids = {c.standardid for c in svc.capabilities}
    assert any(str(i).startswith("ivo://ivoa.net/std/TAP") for i in ids)


def test_tables_endpoint_lists_published_tables(tap_service):
    root = ET.fromstring(httpx.get(f"{tap_service}/tables").text)
    tables = {t.findtext("name") for t in root.iter("table")}
    assert "ska.continuum_sources" in tables
    assert "tap_schema.columns" in tables


def test_tables_metadata_via_pyvo(tap_service):
    svc = pyvo.dal.TAPService(tap_service)
    table = svc.tables["ska.continuum_sources"]
    columns = {c.name: c for c in table.columns}
    assert "ra" in columns and "dec" in columns
    assert columns["ra"].unit == "deg"
    assert columns["ra"].ucd == "pos.eq.ra;meta.main"


def test_everything_tap_schema_publishes_actually_exists(database_url, tap_service):
    """TAP_SCHEMA is the contract a client reads *before* writing a query, so
    an entry with nothing behind it is the service asserting something untrue
    about itself — and the client finds out as a 500 on a query our own
    metadata recommended.

    This is the check that would have caught issue #96, where the benchmark
    generator replaced ivoa.obscore and left six columns advertised that the
    new relation did not have.
    """
    import psycopg
    from egernia_core.metadata.ingest import tap_schema_divergence

    with psycopg.connect(database_url) as conn:
        missing_tables, missing_columns = tap_schema_divergence(conn)

    assert missing_tables == [], f"published but absent: {missing_tables}"
    assert missing_columns == [], f"published but absent: {missing_columns}"


def test_the_divergence_check_actually_detects_one(database_url, tap_service):
    """A check that cannot fail proves nothing, so give it something to find."""
    import psycopg
    from egernia_core.metadata.ingest import tap_schema_divergence

    class _Probe(Exception):
        """Unwinds the transaction: the row was only ever a probe."""

    missing_columns = []
    with psycopg.connect(database_url) as conn, contextlib.suppress(_Probe), conn.transaction():
        conn.execute(
            "INSERT INTO tap_schema.columns"
            " (table_name, column_name, datatype, indexed, principal, std, column_index)"
            " VALUES ('tap_schema.tables', 'not_a_real_column', 'char', 0, 0, 0, 99)"
        )
        _, missing_columns = tap_schema_divergence(conn)
        raise _Probe

    assert "tap_schema.tables.not_a_real_column" in missing_columns
    # and the probe left nothing behind
    with psycopg.connect(database_url) as conn:
        assert tap_schema_divergence(conn) == ([], [])
