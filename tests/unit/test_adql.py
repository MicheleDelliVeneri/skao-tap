"""Unit tests for ADQL translation (queryparser-backed)."""

import pytest
from tapcore.adql import adql_to_postgresql, apply_maxrec, check_language, touched_tables
from tapcore.errors import QueryParseError


def test_plain_select_translates():
    sql = adql_to_postgresql("SELECT table_name FROM tap_schema.tables")
    assert "SELECT table_name" in sql
    assert "tap_schema.tables" in sql


def test_top_becomes_limit():
    sql = adql_to_postgresql("SELECT TOP 5 ra FROM ska.continuum_sources")
    assert "LIMIT 5" in sql


def test_geometry_translates_to_pg_sphere():
    sql = adql_to_postgresql(
        "SELECT ra FROM ska.continuum_sources "
        "WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 62.3, -65.5, 1.0))"
    )
    assert "spoint" in sql
    assert "scircle" in sql


def test_syntax_error_raises_query_parse_error():
    with pytest.raises(QueryParseError):
        adql_to_postgresql("SELEC nonsense FROM nowhere")


def test_non_select_rejected():
    with pytest.raises(QueryParseError):
        adql_to_postgresql("DROP TABLE ska.continuum_sources")


def test_touched_tables_plain():
    sql = adql_to_postgresql(
        "SELECT a.table_name FROM tap_schema.tables AS a "
        "JOIN tap_schema.columns AS b ON a.table_name = b.table_name"
    )
    assert touched_tables(sql) == {"tap_schema.tables", "tap_schema.columns"}


def test_touched_tables_geometry():
    sql = adql_to_postgresql(
        "SELECT ra FROM ska.continuum_sources "
        "WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 62.3, -65.5, 1.0))"
    )
    assert touched_tables(sql) == {"ska.continuum_sources"}


def test_apply_maxrec_wraps_with_one_extra_row():
    wrapped = apply_maxrec("SELECT 1;", 10)
    assert wrapped.startswith("SELECT * FROM (")
    assert wrapped.endswith("LIMIT 11")
    assert ";" not in wrapped


def test_check_language():
    check_language("ADQL")
    check_language("adql-2.0")
    with pytest.raises(QueryParseError):
        check_language("SQL")
