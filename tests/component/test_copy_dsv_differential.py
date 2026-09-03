"""Does the COPY projection produce the bytes `stream_dsv` produces?

This is #106's gate. `copy_dsv` moves DSV serialisation into the server, and
its own condition is byte-for-byte equality: if the bytes differ, latencies
measured before and after are not comparable, and neither are clients' parsers.

Raw `COPY ... FORMAT csv` does *not* produce those bytes — an earlier version of
this file recorded eleven divergences, four of them in float8, which is most of
ObsCore. So `copy_dsv.projection` re-renders every column, and this file is
what says whether each rendering is right. It asks a real PostgreSQL, per column
kind, at boundary values and at NULL.

Where a rendering cannot be reproduced, the module declines the whole result
rather than shipping different bytes; those shapes are asserted as declines at
the bottom rather than as divergences, because a decline is correct behaviour
and a divergence is a bug.

The float cases are the ones worth reading. CPython and PostgreSQL agree on
*which* digits a double has — both write shortest-round-trip — and disagree on
how to lay them out: PostgreSQL turns exponential at 1e15, CPython at 1e16, so
there is a decade where one writes 1000000000000000.0 and the other 1e+15.
`to_char` and `::numeric` cannot bridge it (both round to 15 significant
digits), which is why the projection is a CASE over `::text`.
"""

from __future__ import annotations

import csv
import io

import psycopg
import pytest
from egernia_core.query.copy_dsv import Undecidable, header, projection
from egernia_core.query.results import RowLimiter, columns_from_cursor, stream_dsv

pytestmark = pytest.mark.component


# One column kind per entry: a name, its SQL type, and a probe expression.
# Boundary values rather than pleasant ones — a renderer that agrees on 1 and
# disagrees on -32768 is not agreement.
PROBES = [
    ("bool_true", "bool", "true"),
    ("bool_false", "bool", "false"),
    ("int16_min", "int2", "(-32768)::int2"),
    ("int16_max", "int2", "32767::int2"),
    ("int32_min", "int4", "(-2147483648)::int4"),
    ("int64_max", "int8", "9223372036854775807::int8"),
    # float4 is read back as float(its own text), not as the widened single:
    # 0.1::float4 must print '0.1', not '0.10000000149011612'.
    ("float32", "float4", "1.5::float4"),
    ("float32_fraction", "float4", "0.1::float4"),
    ("float32_integral", "float4", "2.0::float4"),
    ("float32_exponent", "float4", "1e15::float4"),
    ("float32_max", "float4", "3.4e38::float4"),
    ("float64_max", "float8", "1.7976931348623157e308::float8"),
    ("float64_integral", "float8", "2.0::float8"),
    ("float64_neg_zero", "float8", "(-1.0::float8 * 0.0::float8)"),
    # The decade where the two disagree about exponential notation, at both
    # ends, integral and not, and across 2^53 where doubles stop being dense.
    ("float64_below_band", "float8", "1e14::float8"),
    ("float64_band_low", "float8", "1e15::float8"),
    ("float64_band_digits", "float8", "1234567890123456.0::float8"),
    ("float64_band_negative", "float8", "(-1234567890123456.0)::float8"),
    ("float64_band_fraction", "float8", "1000000000000000.5::float8"),
    ("float64_band_eighth", "float8", "1000000000000000.125::float8"),
    ("float64_band_2p53", "float8", "9007199254740994.0::float8"),
    ("float64_band_high", "float8", "9999999999999998.0::float8"),
    ("float64_above_band", "float8", "1e16::float8"),
    # And the small end, where both switch to exponential at the same place.
    ("float64_1e_minus_4", "float8", "1e-4::float8"),
    ("float64_9_9e_minus_5", "float8", "9.9e-5::float8"),
    ("float64_exponent", "float8", "1e-7::float8"),
    ("float64_nan", "float8", "'NaN'::float8"),
    ("float64_inf", "float8", "'Infinity'::float8"),
    ("float64_neg_inf", "float8", "'-Infinity'::float8"),
    ("numeric_trailing_zero", "numeric", "1.10::numeric"),
    ("numeric_big", "numeric", "12345678901234567890.123::numeric"),
    ("date", "date", "'2026-01-02'::date"),
    ("time", "time", "'03:04:05'::time"),
    ("time_micros", "time", "'03:04:05.123456'::time"),
    # Python prints six fractional digits or none; PostgreSQL's text trims
    # trailing zeros, so `time` cannot pass through.
    ("time_half", "time", "'03:04:05.5'::time"),
    ("timestamp", "timestamp", "'2026-01-02 03:04:05'::timestamp"),
    ("timestamp_micros", "timestamp", "'2026-01-02 03:04:05.123456'::timestamp"),
    ("timestamp_millis", "timestamp", "'2026-01-02 03:04:05.123'::timestamp"),
    ("timestamptz", "timestamptz", "'2026-01-02 03:04:05+00'::timestamptz"),
    ("timestamptz_micros", "timestamptz", "'2026-01-02 03:04:05.5+00'::timestamptz"),
    ("text_plain", "text", "'plain'::text"),
    ("text_comma", "text", "'a,b'::text"),
    ("text_tab", "text", "E'a\\tb'::text"),
    ("text_quote", "text", "'say \"hi\"'::text"),
    ("text_newline", "text", "E'line1\\nline2'::text"),
    ("text_carriage_return", "text", "E'line1\\rline2'::text"),
    ("text_empty", "text", "''::text"),
    ("varchar", "varchar", "'vc'::varchar(8)"),
    ("bpchar", "bpchar", "'bp'::char(4)"),
    # psycopg keeps char(n)'s padding; `''::bpchar` compares equal to blanks,
    # so a plain nullif would turn four spaces into NULL.
    ("bpchar_blank", "bpchar", "'    '::char(4)"),
]

# Every probe in one row, plus a text column in front. The multi-column shape
# is deliberate: `csv.writer` writes a *lone* NULL field as `""` so the line is
# not empty, which COPY cannot reproduce and `copy_dsv` therefore declines —
# see test_a_single_column_result_is_declined.
_PAD = "'pad'::text AS pad"


def _probe_sql(include_nulls: bool) -> str:
    cells = ", ".join(f"{expr} AS {name}" for name, _, expr in PROBES)
    nulls = ", ".join(f"NULL::{sql_type} AS {name}" for name, sql_type, _ in PROBES)
    if not include_nulls:
        return f"SELECT {_PAD}, {cells}"
    return f"SELECT {_PAD}, {cells} UNION ALL SELECT NULL::text AS pad, {nulls}"


def _via_writer(conn, sql: str, delimiter: str = ",") -> bytes:
    """What the service sends today."""
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = columns_from_cursor(cur.description, {})
        return b"".join(stream_dsv(columns, RowLimiter(cur.fetchall(), 1000), delimiter))


def _via_copy(conn, sql: str, delimiter: str = ",") -> bytes:
    """What the service sends after #106."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM ({sql}) AS probe LIMIT 0")
        columns = columns_from_cursor(cur.description, {})
        projected = projection(cur.description, sql)
    literal = "E'\\t'" if delimiter == "\t" else "','"
    with (
        conn.cursor() as cur,
        cur.copy(f"COPY ({projected}) TO STDOUT WITH (FORMAT csv, DELIMITER {literal})") as copy,
    ):
        return header(columns, delimiter) + b"".join(bytes(chunk) for chunk in copy)


def _column(body: bytes, name: str, delimiter: str = ",") -> list[str]:
    """One named column's cells, so a failure names the value not the row."""
    header_row, *data = list(csv.reader(io.StringIO(body.decode()), delimiter=delimiter))
    index = header_row.index(name)
    return [row[index] for row in data]


@pytest.fixture(scope="module")
def conn(database_url):
    # autocommit: a probe that is *meant* to fail (an unhandled type)
    # would otherwise abort the transaction and every later test with it.
    with psycopg.connect(database_url, autocommit=True) as connection:
        yield connection


def test_copy_is_available(conn):
    """First on purpose: if `cur.copy()` did not work, every comparison below
    would fail for a reason that has nothing to do with rendering."""
    with (
        conn.cursor() as cur,
        cur.copy("COPY (SELECT 1 AS a, 'x'::text AS b) TO STDOUT WITH (FORMAT csv)") as copy,
    ):
        assert b"".join(bytes(chunk) for chunk in copy) == b"1,x\n"


def test_the_header_row_matches(conn):
    """The header is written by the same writer on both paths, so this is
    really a check that the projection did not rename anything."""
    sql = _probe_sql(include_nulls=False)
    assert _via_writer(conn, sql).splitlines()[0] == _via_copy(conn, sql).splitlines()[0]


@pytest.mark.parametrize("name", [p[0] for p in PROBES])
def test_each_column_kind_renders_identically(conn, name):
    """Per column, so a failure names the kind rather than diffing two blobs."""
    sql = _probe_sql(include_nulls=False)
    written = _column(_via_writer(conn, sql), name)
    copied = _column(_via_copy(conn, sql), name)
    assert written == copied, f"{name}: stream_dsv wrote {written!r}, COPY wrote {copied!r}"


@pytest.mark.parametrize("name", [p[0] for p in PROBES])
def test_null_renders_identically(conn, name):
    """NULL is the value most likely to differ and least likely to be noticed.

    A quoted empty string and an unquoted empty field are different bytes and
    only one of them means NULL to a parser, so this is asserted rather than
    assumed.
    """
    sql = _probe_sql(include_nulls=True)
    written = _column(_via_writer(conn, sql), name)[-1]
    copied = _column(_via_copy(conn, sql), name)[-1]
    assert written == copied == "", f"{name}: NULL rendered {written!r} vs {copied!r}"


def test_the_whole_body_is_identical(conn):
    """The per-column tests parse with `csv.reader`, which normalises a quoting
    difference away — two encodings of the same value parse to the same cell.
    This compares the bytes, values and NULLs together."""
    sql = _probe_sql(include_nulls=True)
    assert _via_writer(conn, sql) == _via_copy(conn, sql)


def test_tsv_is_identical_too(conn):
    """The other DSV. Tabs inside values are the reason it is not assumed:
    each writer decides on its own when to quote."""
    sql = _probe_sql(include_nulls=True)
    assert _via_writer(conn, sql, "\t") == _via_copy(conn, sql, "\t")


def test_quoting_agrees_on_the_awkward_strings(conn):
    """Commas, quotes, newlines and a carriage return inside values, as raw
    bytes. `\\r` is here because `csv.writer` and COPY decide separately
    whether it forces quoting."""
    sql = (
        "SELECT 'pad'::text AS pad, 'a,b'::text AS c1, 'say \"hi\"'::text AS c2,"
        " E'l1\\nl2'::text AS c3, E'l1\\rl2'::text AS c4, E'a\\tb'::text AS c5"
    )
    assert _via_writer(conn, sql) == _via_copy(conn, sql)


def test_an_unknown_type_is_declined(conn):
    """A type with no rendering must decline the result, not guess at one.

    `bytea` here because it is always available, but pg_sphere's `spoly` is the
    case that matters in practice: it is the footprint column of every
    published ObsCore table, and the writer sends it through `_plain`, which
    hex-encodes bytes and JSON-encodes containers — none of which COPY does.
    """
    sql = "SELECT 'pad'::text AS pad, '\\x0001'::bytea AS b"
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM ({sql}) AS probe LIMIT 0")
        with pytest.raises(Undecidable) as raised:
            projection(cur.description, sql)
    assert raised.value.reason == "unknown_type"


def test_every_probe_type_is_actually_covered(conn):
    """The probes must exercise the projection, not the passthrough by accident.

    Without this, deleting an expression from `_EXPRESSIONS` would move its
    type to "unknown" and every test above would still pass — because
    `projection` would raise and the parametrised tests would error rather
    than compare, which is a red suite but not an obvious one.
    """
    sql = _probe_sql(include_nulls=False)
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM ({sql}) AS probe LIMIT 0")
        oids = {entry.type_code for entry in cur.description}
        projection(cur.description, sql)  # raises Undecidable if any is unhandled
    assert len(oids) >= 12, f"only {len(oids)} distinct type OIDs probed: {sorted(oids)}"


def test_a_single_column_result_is_declined(conn):
    """One column is the shape `csv.writer` renders unlike anything COPY can.

    A lone NULL field becomes `""` there, so that the line is not empty; COPY
    writes nothing, and its CSV quoting cannot be asked for the other spelling
    — a NULL specification containing the quote character is rejected by the
    server outright. So the shape is declined rather than approximated.
    """
    from egernia_core.query.copy_dsv import stream_copy_dsv

    sql = "SELECT NULL::float8 AS v"
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM ({sql}) AS probe LIMIT 0")
        columns = columns_from_cursor(cur.description, {})
        with pytest.raises(Undecidable) as raised:
            stream_copy_dsv(cur, sql, columns, cur.description, ",", 100)
    assert raised.value.reason == "single_column"
    # and the writer really does write it that way, which is why
    assert _via_writer(conn, sql).splitlines()[1] == b'""'


def test_repeated_column_names_are_declined(conn):
    """Two columns with one name. The projection renames positionally so it
    could in fact render them, but the header could not be trusted to mean the
    same thing on both paths, so the result is declined."""
    from egernia_core.query.copy_dsv import stream_copy_dsv

    sql = "SELECT 1::int4 AS a, 2::int4 AS a"
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM ({sql}) AS probe LIMIT 0")
        columns = columns_from_cursor(cur.description, {})
        with pytest.raises(Undecidable) as raised:
            stream_copy_dsv(cur, sql, columns, cur.description, ",", 100)
    assert raised.value.reason == "duplicate_column"
