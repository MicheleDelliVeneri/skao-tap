"""CPU-focused benchmarks for the TAP query hot paths.

These benchmarks deliberately avoid network and database I/O so CI runs can
be compared without mixing in service or storage latency. End-to-end database performance is
covered by the separate PostgreSQL performance workflow.
"""

import datetime
from decimal import Decimal

import pytest

# pytest collects everything under tests/ (see pyproject.toml testpaths), and
# the benchmark fixture comes from the plugin — without it every test here is
# an error on a plain `pytest` run. Skip the module instead.
pytest.importorskip("pytest_benchmark", reason="benchmarks need pytest-benchmark")

from queryparser.adql import ADQLQueryTranslator
from tapcore.query.adql import adql_to_postgresql, touched_tables, translate
from tapcore.query.results import ColumnMeta, RowLimiter, stream

CONE_SEARCH = (
    "SELECT source_id, ra, dec, flux FROM ska.continuum_sources "
    "WHERE 1=CONTAINS(POINT('ICRS', ra, dec), "
    "CIRCLE('ICRS', 62.3, -65.5, 1.0))"
)
JOIN_QUERY = (
    "SELECT s.source_id, s.ra, s.dec, t.table_name "
    "FROM ska.continuum_sources AS s "
    "JOIN tap_schema.tables AS t ON t.table_name = 'ska.continuum_sources'"
)
COLUMNS = [
    ColumnMeta("source_id", kind="int64", ucd="meta.id"),
    ColumnMeta("name", kind="str"),
    ColumnMeta("flux", kind="float64", unit="mJy"),
    ColumnMeta("observed_at", kind="timestamp"),
    ColumnMeta("validated", kind="bool"),
]
ROWS = tuple(
    (
        index,
        f"source-{index}",
        Decimal(f"{index % 100}.{index % 10}"),
        datetime.datetime(2026, 1, 1) + datetime.timedelta(seconds=index),
        index % 2 == 0,
    )
    for index in range(1_000)
)


def _translate_and_inspect() -> set[str]:
    """The old shape, spelled out rather than routed through translate().

    adql_to_postgresql() now walks the parse tree as well, so calling it here
    would measure today's work plus the re-parse and stop being comparable to
    the historical baseline this exists to sit beside.
    """
    translator = ADQLQueryTranslator(JOIN_QUERY)
    return touched_tables(translator.to_postgresql())


def _translate_single_pass() -> set[str]:
    """What the service does now: both from one parse."""
    return set(translate(JOIN_QUERY).tables)


def _serialize(fmt: str) -> bytes:
    limiter = RowLimiter(iter(ROWS), len(ROWS))
    return b"".join(stream(COLUMNS, limiter, fmt))


def test_benchmark_adql_geometry_translation(benchmark):
    sql = benchmark(adql_to_postgresql, CONE_SEARCH).lower()
    assert "spoint" in sql
    assert "scircle" in sql


def test_benchmark_adql_translation_and_table_inspection(benchmark):
    """Kept as the reference the single-pass benchmark below is measured
    against — this is what a request used to cost."""
    tables = benchmark(_translate_and_inspect)
    assert tables == {"ska.continuum_sources", "tap_schema.tables"}


def test_benchmark_adql_translation_single_pass(benchmark):
    tables = benchmark(_translate_single_pass)
    assert tables == {"ska.continuum_sources", "tap_schema.tables"}


def test_benchmark_votable_serialization(benchmark):
    body = benchmark(_serialize, "votable")
    assert body.startswith(b"<?xml")
    assert body.count(b"<TR>") == len(ROWS)


def test_benchmark_json_serialization(benchmark):
    body = benchmark(_serialize, "json")
    assert b'"status":"OK"' in body.replace(b" ", b"")
