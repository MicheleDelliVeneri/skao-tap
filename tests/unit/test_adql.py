"""Unit tests for ADQL translation (backed by the vendored ADQL 2.1 fork
of queryparser in egernia_core.query._adql)."""

import pytest
from egernia_core.errors import QueryParseError
from egernia_core.query.adql import adql_to_postgresql, apply_maxrec, check_language, touched_tables


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
    check_language("ADQL-2.1")
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
    # ADQL 2.1 shapes: the fast path must agree on the new grammar too
    "SELECT obs_id FROM ivoa.obscore WHERE 1=INTERSECTS(s_region_geom, CIRCLE('ICRS', 1, -3, 0.5))",
    "SELECT CAST(flux AS DOUBLE PRECISION) FROM ska.continuum_sources",
    "SELECT LOWER(obs_collection) FROM ivoa.obscore WHERE obs_collection ILIKE 'ska%'",
    "SELECT ra FROM ska.continuum_sources ORDER BY ra OFFSET 100",
    "SELECT a FROM t UNION SELECT a FROM u",
)


@pytest.mark.parametrize("query", SHAPES)
def test_the_fast_path_translates_exactly_as_the_library_does(query):
    """SLL prediction is only sound if it produces the same tree. Verified
    here per shape, and separately across the whole 12,000-query benchmark
    corpus — this is the regression guard for a grammar or library bump."""
    from egernia_core.query._adql import ADQLQueryTranslator

    assert adql_to_postgresql(query) == ADQLQueryTranslator(query).to_postgresql()


def test_translation_parses_once(monkeypatch):
    """The library parses in set_query() and again in to_postgresql(),
    discarding the first tree. That doubling was half the cost of every
    request."""
    from egernia_core.query import adql as adql_module

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
    from egernia_core.observability import ADQL_SLOW_PARSES
    from egernia_core.query import adql as adql_module

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
    from egernia_core.observability import ADQL_SLOW_PARSES

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
    from egernia_core.query.adql import translate

    result = translate(
        "SELECT o.obs_id FROM caom.observation AS o JOIN caom.plane AS p ON o.obs_id = p.obs_id"
    )
    assert result.tables == frozenset({"caom.observation", "caom.plane"})


# ---------------------------------------------------------------------------
# Geometry-typed columns as INTERSECTS/CONTAINS arguments
#
# Originally supported (package 7) by hiding the column from the 2.0 grammar
# behind a sentinel POLYGON literal and swapping the emitted pgsphere literal
# back afterwards. The vendored 2.1 grammar accepts the column directly
# (package 21), so these now pin the grammar-native translation — the
# assertions are unchanged from the sentinel era on purpose.
# ---------------------------------------------------------------------------


def test_geometry_column_as_intersects_argument():
    from egernia_core.query.adql import translate

    result = translate(
        "SELECT obs_id FROM srcnet.data_products"
        " WHERE 1=INTERSECTS(s_region_geom, CIRCLE('ICRS', 10.5, -30.2, 0.5))"
    )
    assert (
        "s_region_geom && scircle(spoint(RADIANS(10.5), RADIANS(-30.2)), RADIANS(0.5))"
        in result.sql
    )
    assert result.tables == frozenset({"srcnet.data_products"})


def test_geometry_column_in_either_argument_position():
    from egernia_core.query.adql import translate

    first = translate(
        "SELECT 1 FROM srcnet.artifacts WHERE 1=INTERSECTS(s_region_geom, CIRCLE('ICRS',1,2,0.1))"
    ).sql
    second = translate(
        "SELECT 1 FROM srcnet.artifacts WHERE 1=INTERSECTS(CIRCLE('ICRS',1,2,0.1), s_region_geom)"
    ).sql
    assert "s_region_geom &&" in first
    assert "&& s_region_geom" in second


def test_point_in_geometry_column_via_contains():
    from egernia_core.query.adql import translate

    sql = translate(
        "SELECT 1 FROM srcnet.artifacts AS a WHERE 1=CONTAINS(POINT('ICRS', 1, 2), a.s_region_geom)"
    ).sql
    assert "spoint(RADIANS(1.0), RADIANS(2.0)) @ a.s_region_geom" in sql


def test_two_geometry_columns_in_one_query():
    from egernia_core.query.adql import translate

    sql = translate(
        "SELECT 1 FROM srcnet.artifacts WHERE"
        " 1=INTERSECTS(s_region_geom, POLYGON('ICRS', 1,2, 3,4, 5,6))"
        " AND 1=CONTAINS(POINT('ICRS', 5, 6), s_region_geom)"
    ).sql
    assert sql.count("s_region_geom") == 2


def test_constructor_arguments_are_left_alone():
    from egernia_core.query.adql import translate

    sql = translate(
        "SELECT TOP 5 obs_id FROM ivoa.obscore"
        " WHERE 1=CONTAINS(POINT('ICRS', s_ra, s_dec), CIRCLE('ICRS', 10.5, -30.2, 0.5))"
    ).sql
    assert "@ scircle" in sql


def test_predicate_names_inside_string_literals_are_not_rewritten():
    from egernia_core.query.adql import translate

    sql = translate(
        "SELECT obs_id FROM ivoa.obscore WHERE obs_collection = 'INTERSECTS(trap, x)'"
    ).sql
    assert "'INTERSECTS(trap, x)'" in sql


# ---------------------------------------------------------------------------
# ADQL 2.1 conformance (package 21) — one test per construct the service
# declares in /capabilities, plus the deliberate refusals. The declaration
# is only truthful while these pass.
# ---------------------------------------------------------------------------


def test_area_of_a_geometry_column():
    sql = adql_to_postgresql("SELECT AREA(s_region_geom) FROM ivoa.obscore")
    assert "square_degrees(area(s_region_geom))" in sql


def test_distance_between_point_columns():
    sql = adql_to_postgresql("SELECT DISTANCE(p1, p2) FROM srcnet.artifacts")
    assert "DEGREES(p1 <-> p2)" in sql


def test_point_column_as_circle_center():
    sql = adql_to_postgresql("SELECT 1 FROM t WHERE 1=CONTAINS(pt_col, CIRCLE(center_col, 0.5))")
    assert "pt_col @ scircle(center_col, RADIANS(0.5))" in sql


def test_coordinate_system_is_optional():
    """2.1 deprecates the coordinate-system argument; constructors must
    accept its omission and, for 2.0 compatibility, an empty string."""
    omitted = adql_to_postgresql("SELECT 1 FROM t WHERE 1=CONTAINS(POINT(1,2), CIRCLE(1,2,0.1))")
    empty = adql_to_postgresql(
        "SELECT 1 FROM t WHERE 1=CONTAINS(POINT('',1,2), CIRCLE('',1,2,0.1))"
    )
    explicit = adql_to_postgresql(
        "SELECT 1 FROM t WHERE 1=CONTAINS(POINT('ICRS',1,2), CIRCLE('ICRS',1,2,0.1))"
    )
    assert omitted == empty == explicit


@pytest.mark.parametrize(
    "target,sql_type",
    [
        ("SMALLINT", "SMALLINT"),
        ("INTEGER", "INTEGER"),
        ("BIGINT", "BIGINT"),
        ("REAL", "REAL"),
        ("DOUBLE PRECISION", "DOUBLE PRECISION"),
        ("CHAR(2)", "CHAR (2)"),
        ("VARCHAR(10)", "VARCHAR (10)"),
        ("TIMESTAMP", "TIMESTAMP"),
    ],
)
def test_cast_renders_as_itself(target, sql_type):
    sql = adql_to_postgresql(f"SELECT CAST(flux AS {target}) FROM t")
    assert f"CAST(flux AS {sql_type})" in sql


def test_cast_to_a_geometry_type_is_a_clear_error():
    """The 2.1 grammar admits geometry cast targets, but no pgsphere mapping
    exists — better a parse-time error than SQL that fails in the database."""
    with pytest.raises(QueryParseError, match="geometry"):
        adql_to_postgresql("SELECT CAST(s_region_geom AS POINT) FROM t")


def test_lower_and_upper_accept_expressions():
    sql = adql_to_postgresql("SELECT LOWER(a) FROM t WHERE UPPER(t.b) = 'X'")
    assert "LOWER(a)" in sql
    assert "UPPER(t.b)" in sql


def test_ilike_passes_through():
    sql = adql_to_postgresql("SELECT a FROM t WHERE a ILIKE 'ska%'")
    assert "ILIKE 'ska%'" in sql


def test_coalesce_passes_through():
    sql = adql_to_postgresql("SELECT COALESCE(a, b, 0) FROM t")
    assert "COALESCE(a, b, 0)" in sql


def test_offset_passes_through():
    sql = adql_to_postgresql("SELECT a FROM t ORDER BY a OFFSET 100")
    assert sql.rstrip(";").endswith("OFFSET 100")


def test_set_operators_pass_through():
    from egernia_core.query.adql import translate

    for op in ("UNION", "EXCEPT", "INTERSECT"):
        result = translate(f"SELECT a FROM t {op} SELECT a FROM u")
        assert f" {op} " in result.sql
        # the publication check must see both sides
        assert result.tables == frozenset({"t", "u"})


def test_bitwise_xor_becomes_postgres_hash():
    """ADQL spells XOR '^', which is exponentiation in PostgreSQL — passing
    it through would compute powers. (Bitwise operators were dropped from the
    final 2.1 REC, so they are an undeclared extension; they must still not
    produce wrong answers.)"""
    sql = adql_to_postgresql("SELECT a ^ 3 FROM t WHERE b & 1 = 0")
    assert "a # 3" in sql
    assert "b & 1" in sql


def test_hexadecimal_literals_render_in_decimal():
    """Bare 0x… literals need PostgreSQL 16+; decimal works everywhere."""
    sql = adql_to_postgresql("SELECT 0x1F FROM t WHERE a = 0xff")
    assert "SELECT 31" in sql
    assert "= 255" in sql


def test_with_is_still_refused():
    """Common table expressions are not declared in /capabilities, so the
    refusal must stay a clear error rather than becoming silent breakage."""
    with pytest.raises(QueryParseError, match="WITH"):
        adql_to_postgresql("WITH x AS (SELECT a FROM t) SELECT a FROM x")


# ---------------------------------------------------------------------------
# Geometry-slot columns are reported on the Translation (package 22 enabler).
# Translation is pure — no TAP_SCHEMA — so a *text* column in a geometry slot
# (ObsCore's s_region) translates cleanly here and would die in PostgreSQL
# with "operator does not exist: text && scircle". The type check lives with
# the caller that has column metadata; this list is what it checks.
# ---------------------------------------------------------------------------


def test_geometry_slot_columns_are_reported():
    from egernia_core.query.adql import translate

    result = translate(
        "SELECT obs_id FROM ivoa.obscore"
        " WHERE 1=INTERSECTS(s_region, CIRCLE('ICRS', 150.0, -30.0, 0.5))"
    )
    # The translator accepts it — the type check is the caller's, from here:
    assert result.geometry_columns == frozenset({"s_region"})


def test_geometry_slot_columns_in_either_argument_position():
    from egernia_core.query.adql import translate

    for query in (
        "SELECT 1 FROM t WHERE 1=INTERSECTS(footprint, CIRCLE('ICRS',1,2,0.1))",
        "SELECT 1 FROM t WHERE 1=INTERSECTS(CIRCLE('ICRS',1,2,0.1), footprint)",
        "SELECT 1 FROM t WHERE 1=CONTAINS(footprint, CIRCLE('ICRS',1,2,0.1))",
        "SELECT 1 FROM t WHERE 1=CONTAINS(CIRCLE('ICRS',1,2,0.1), footprint)",
    ):
        assert translate(query).geometry_columns == frozenset({"footprint"})


def test_geometry_slot_columns_from_every_accepting_construct():
    from egernia_core.query.adql import translate

    area = translate("SELECT AREA(a.footprint) FROM t AS a")
    assert area.geometry_columns == frozenset({"a.footprint"})

    distance = translate("SELECT DISTANCE(p1, p2) FROM t")
    assert distance.geometry_columns == frozenset({"p1", "p2"})

    centre = translate("SELECT 1 FROM t WHERE 1=CONTAINS(pt_col, CIRCLE(center_col, 0.5))")
    assert centre.geometry_columns == frozenset({"pt_col", "center_col"})


def test_all_literal_geometry_reports_no_columns():
    from egernia_core.query.adql import translate

    result = translate(
        "SELECT TOP 5 obs_id FROM ivoa.obscore"
        " WHERE 1=CONTAINS(POINT('ICRS', s_ra, s_dec), CIRCLE('ICRS', 10.5, -30.2, 0.5))"
    )
    # s_ra/s_dec are constructor *coordinates*, not geometry-slot columns.
    assert result.geometry_columns == frozenset()
