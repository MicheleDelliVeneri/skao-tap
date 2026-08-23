"""Unit tests for ADQL translation (queryparser-backed)."""

import pytest
from tapcore.errors import QueryParseError
from tapcore.query.adql import adql_to_postgresql, apply_maxrec, check_language, touched_tables


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


# -- the fast translation path ----------------------------------------------
#
# Translation is where essentially all of a request's CPU goes: measured on a
# 12,000-query corpus it was 41 ms of a ~50 ms request, so the service's
# single-core throughput ceiling is set here. Two things made it slow, and both
# fixes need pinning: the library parsed twice per translation and threw the
# first tree away, and ANTLR's default full-context prediction was 71% of the
# profile. What must never change is the *output*.

SHAPES = (
    "SELECT table_name FROM tap_schema.tables",
    "SELECT TOP 10 ra, dec FROM ska.continuum_sources",
    "SELECT ra FROM ska.continuum_sources WHERE flux > 1.5 AND ra < 100",
    "SELECT ra FROM ska.continuum_sources WHERE 1=CONTAINS("
    "POINT('ICRS', ra, dec), CIRCLE('ICRS', 62.3, -65.5, 1.0))",
    "SELECT ra FROM ska.continuum_sources WHERE 1=INTERSECTS("
    "CIRCLE('ICRS', ra, dec, 0.1), CIRCLE('ICRS', 10.0, 20.0, 2.0))",
    "SELECT TOP 100 s.ra, s.dec FROM ska.continuum_sources AS s "
    "WHERE s.source_id = 42 ORDER BY s.ra",
    "SELECT o.obs_id, p.plane_id FROM caom.observation AS o "
    "JOIN caom.plane AS p ON o.obs_id = p.obs_id WHERE o.collection = 'X'",
    "SELECT collection, COUNT(*) FROM caom.observation GROUP BY collection",
    "SELECT DISTANCE(POINT('ICRS', 10, 20), POINT('ICRS', 11, 21)) FROM ska.continuum_sources",
    "SELECT ra FROM ska.continuum_sources WHERE dec BETWEEN -10 AND 10",
)


@pytest.mark.parametrize("query", SHAPES)
def test_the_fast_path_translates_exactly_as_the_library_does(query):
    """SLL prediction is only sound if it produces the same tree. Verified
    here per shape, and separately across the whole 12,000-query benchmark
    corpus — this is the regression guard for a grammar or library bump."""
    from queryparser.adql import ADQLQueryTranslator

    assert adql_to_postgresql(query) == ADQLQueryTranslator(query).to_postgresql()


def test_translation_parses_once(monkeypatch):
    """The library parses in set_query() and again in to_postgresql(),
    discarding the first tree. That doubling was half the cost of every
    request."""
    from tapcore.query import adql as adql_module

    calls = []
    original = adql_module._Translator._parse_sll

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(adql_module._Translator, "_parse_sll", counting)
    adql_module.translate("SELECT TOP 5 ra FROM ska.continuum_sources")
    assert len(calls) == 1


def test_a_query_the_fast_path_cannot_handle_falls_back(monkeypatch):
    """The fast path is an optimisation, not a replacement: anything it trips
    over has to get the library's own full-context parse and the same answer.
    Forced here rather than waiting for an ambiguous grammar to appear."""
    from tapcore.observability import ADQL_SLOW_PARSES
    from tapcore.query import adql as adql_module

    query = "SELECT TOP 3 ra, dec FROM ska.continuum_sources WHERE flux > 2"
    expected = adql_to_postgresql(query)

    def unusable(self):
        raise RuntimeError("pretend SLL could not decide")

    monkeypatch.setattr(adql_module._Translator, "_parse_sll", unusable)
    before = ADQL_SLOW_PARSES._value.get()
    assert adql_module.translate(query).sql == expected
    # The fallback is counted, so a fast path that stops working is visible
    # rather than just slow.
    assert ADQL_SLOW_PARSES._value.get() == before + 1


def test_an_invalid_query_is_not_counted_as_a_slow_parse():
    """The counter's stated meaning is "the fast path stopped working". An
    invalid query bails out of SLL too, and counting it would let a burst of
    bad ADQL read as a performance regression."""
    from tapcore.observability import ADQL_SLOW_PARSES

    before = ADQL_SLOW_PARSES._value.get()
    with pytest.raises(QueryParseError):
        adql_to_postgresql("SELECT FROM WHERE")
    assert ADQL_SLOW_PARSES._value.get() == before


def test_a_syntax_error_survives_the_fast_path():
    """An invalid query must still be reported as one, with the library's own
    error rather than whatever the bail strategy raised first."""
    with pytest.raises(QueryParseError, match="syntax error"):
        adql_to_postgresql("SELECT FROM WHERE")


def test_tables_come_from_the_single_parse():
    from tapcore.query.adql import translate

    result = translate(
        "SELECT o.obs_id FROM caom.observation AS o JOIN caom.plane AS p ON o.obs_id = p.obs_id"
    )
    assert result.tables == frozenset({"caom.observation", "caom.plane"})
