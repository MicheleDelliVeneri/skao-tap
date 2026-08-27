"""Does PostgreSQL's `COPY ... FORMAT csv` produce the bytes `stream_dsv` does?

The gate for #106. That package proposes moving DSV serialisation into the
server — `COPY (<query>) TO STDOUT WITH (FORMAT csv, HEADER)` instead of a
Python `csv.writer` over materialised row tuples — to recover the 3.90 ms/request
those two subsystems cost. Its own condition is byte-for-byte equality: if the
bytes differ, latencies measured before and after are not comparable, and
neither are clients' parsers.

This file answers that question against a real PostgreSQL, per column kind, at
boundary values and NULL, *before* any COPY path exists in the service. Where the
two agree, the test asserts it. Where they disagree, an `xfail(strict=True)`
records the divergence and the reason — so the divergence is written down in code
rather than in a comment, and the moment a SQL projection fixes it the strict
xfail turns the XPASS into a failure and forces this file to be updated.

The issue is explicit that divergences must be fixed in the projection (a cast
in the statement) and not patched afterwards, because a per-row Python pass to
repair the bytes would reintroduce the cost the change exists to remove. So each
xfail below names the cast that would close it.
"""

from __future__ import annotations

import csv
import io

import psycopg
import pytest
from egernia_core.query.results import RowLimiter, columns_from_cursor, stream_dsv

pytestmark = pytest.mark.component


# One column kind per row of this table: the SQL type, a probe expression, and
# what makes it worth probing. Boundary values rather than pleasant ones —
# a writer that agrees on 1 and disagrees on -32768 is not agreement.
PROBES = [
    ("bool_true", "bool", "true"),
    ("bool_false", "bool", "false"),
    ("int16_min", "int2", "(-32768)::int2"),
    ("int16_max", "int2", "32767::int2"),
    ("int32_min", "int4", "(-2147483648)::int4"),
    ("int64_max", "int8", "9223372036854775807::int8"),
    ("float32", "float4", "1.5::float4"),
    ("float64", "float8", "1.7976931348623157e308::float8"),
    ("float_neg_zero", "float8", "(-0.0)::float8"),
    ("float_integral", "float8", "2.0::float8"),
    ("float32_integral", "float4", "2.0::float4"),
    ("float_exponent", "float8", "1e-7::float8"),
    ("float_nan", "float8", "'NaN'::float8"),
    ("float_inf", "float8", "'Infinity'::float8"),
    ("numeric_trailing_zero", "numeric", "1.10::numeric"),
    ("numeric_big", "numeric", "12345678901234567890.123::numeric"),
    ("date", "date", "'2026-01-02'::date"),
    ("time", "time", "'03:04:05'::time"),
    ("timestamp", "timestamp", "'2026-01-02 03:04:05'::timestamp"),
    ("timestamptz", "timestamptz", "'2026-01-02 03:04:05+00'::timestamptz"),
    ("text_plain", "text", "'plain'::text"),
    ("text_comma", "text", "'a,b'::text"),
    ("text_quote", "text", "'say \"hi\"'::text"),
    ("text_newline", "text", "E'line1\\nline2'::text"),
    ("text_empty", "text", "''::text"),
    ("varchar", "varchar", "'vc'::varchar(8)"),
    ("bpchar", "bpchar", "'bp'::char(4)"),
]

# Kinds the service coerces on its way out, so the two renderings cannot match
# without a cast in the projection. Each entry is the reason and the fix.
# What actually differs, measured rather than predicted. Each entry is the cause
# and what would close it. Two guesses were wrong when this was first run and
# are worth recording: `date` and `time` agree (removed from this list by the
# exhaustiveness test below), and float8/float4 diverge in four separate ways
# that #106 does not mention at all.
KNOWN_DIVERGENCES = {
    "bool_true": (
        "csv.writer renders Python True as 'True'; COPY renders bool as 't'. "
        "Projection fix: CASE WHEN col THEN 'true' ELSE 'false' END, or accept "
        "t/f as the wire form."
    ),
    "bool_false": "As bool_true: 'False' against 'f'.",
    # ---- float8/float4: the surface #106 does not name -------------------
    # These matter most. ObsCore's s_ra, s_dec, t_min, t_max, em_min, em_max
    # and t_exptime are all float8, so this is the common case, not an edge.
    # `::float8` cannot close any of them: the divergence is float-to-text
    # rendering, server against CPython, not a type coercion.
    "float_integral": (
        "Python str(2.0) is '2.0'; PostgreSQL renders float8 2 as '2'. Every "
        "whole-numbered float differs. Closing it needs to_char or an explicit "
        "format per float column, which is server-side cost the package would "
        "have to price."
    ),
    "float32_integral": "As float_integral, for float4.",
    "float_neg_zero": (
        "str(-0.0) is '0.0' after psycopg hands back a float; COPY renders '0'. "
        "The same rendering difference as float_integral, at zero."
    ),
    "float_nan": "Python 'nan' against PostgreSQL 'NaN'.",
    "float_inf": "Python 'inf' against PostgreSQL 'Infinity'.",
    # ---- numeric and timestamps: the ones #106 anticipated ---------------
    "numeric_trailing_zero": (
        "_VALUE_COERCIONS maps decimal->float, so 1.10 becomes '1.1'; COPY "
        "preserves numeric's scale and renders '1.10'. Projection fix: "
        "col::float8 — but note that lands in the float cases above."
    ),
    "numeric_big": (
        "decimal->float loses precision beyond float8; COPY renders the exact "
        "numeric. A ::float8 cast makes the bytes match by degrading them, "
        "which is a decision for the package rather than a detail."
    ),
    "timestamp": (
        "_isoformat uses the 'T' separator ('2026-01-02T03:04:05'); COPY uses a "
        "space. Projection fix: to_char(col, 'YYYY-MM-DD\"T\"HH24:MI:SS')."
    ),
    "timestamptz": (
        "_isoformat gives '2026-01-02T03:04:05+00:00'; COPY gives "
        "'2026-01-02 03:04:05+00'. Projection fix: to_char with an explicit "
        "offset format, and the offset spelling has to be chosen deliberately."
    ),
}


def _probe_sql(include_nulls: bool) -> str:
    """A one- or two-row query with every probe as a column."""
    cells = ", ".join(f"{expr} AS {name}" for name, _, expr in PROBES)
    typed_nulls = ", ".join(f"NULL::{sql_type} AS {name}" for name, sql_type, _ in PROBES)
    if not include_nulls:
        return f"SELECT {cells}"
    return f"SELECT {cells} UNION ALL SELECT {typed_nulls}"


def _dsv_via_writer(conn: psycopg.Connection, sql: str) -> bytes:
    """What the service sends today."""
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = columns_from_cursor(cur.description, {})
        rows = RowLimiter(cur.fetchall(), maxrec=1000)
        return b"".join(stream_dsv(columns, rows, delimiter=","))


def _dsv_via_copy(conn: psycopg.Connection, sql: str) -> bytes:
    """What #106 proposes to send instead."""
    chunks: list[bytes] = []
    with (
        conn.cursor() as cur,
        cur.copy(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER)") as copy,
    ):
        for chunk in copy:
            chunks.append(bytes(chunk))
    return b"".join(chunks)


def _column(body: bytes, name: str) -> list[str]:
    """One named column's cells, so a failure names the value not the whole row."""
    rows = list(csv.reader(io.StringIO(body.decode())))
    header, *data = rows
    index = header.index(name)
    return [row[index] for row in data]


@pytest.fixture(scope="module")
def conn(database_url):
    with psycopg.connect(database_url) as connection:
        yield connection


def test_copy_is_available_and_produces_a_header(conn):
    """Before comparing anything: COPY works and HEADER means what we think.

    First test on purpose. If `cur.copy()` were unavailable or the header were
    absent, every comparison below would fail for a reason that has nothing to
    do with rendering.
    """
    body = _dsv_via_copy(conn, "SELECT 1 AS a, 'x'::text AS b")
    assert body.splitlines()[0] == b"a,b"
    assert body.splitlines()[1] == b"1,x"


def test_the_header_row_matches(conn):
    """Column names are the service's own, not PostgreSQL's rendering of them."""
    sql = _probe_sql(include_nulls=False)
    assert _dsv_via_writer(conn, sql).splitlines()[0] == _dsv_via_copy(conn, sql).splitlines()[0]


@pytest.mark.parametrize("name", [p[0] for p in PROBES])
def test_each_column_kind_renders_identically(conn, name, request):
    """Per column, so a failure names the kind rather than a diff of two blobs."""
    if name in KNOWN_DIVERGENCES:
        request.node.add_marker(pytest.mark.xfail(strict=True, reason=KNOWN_DIVERGENCES[name]))
    sql = _probe_sql(include_nulls=False)
    written = _column(_dsv_via_writer(conn, sql), name)
    copied = _column(_dsv_via_copy(conn, sql), name)
    assert written == copied, (
        f"{name}: stream_dsv produced {written!r}, COPY produced {copied!r}. "
        "Per #106 this has to be closed by a cast in the projection, not by a "
        "per-row pass afterwards."
    )


@pytest.mark.parametrize("name", [p[0] for p in PROBES])
def test_null_renders_identically(conn, name, request):
    """NULL is the value most likely to differ and least likely to be noticed.

    csv.writer writes an empty field for None; COPY writes an empty field for
    NULL in CSV format. They agree — but a quoted empty string and an unquoted
    empty field are different bytes, and only one of them means NULL to a
    parser, so it is asserted rather than assumed.
    """
    del request
    sql = _probe_sql(include_nulls=True)
    written = _column(_dsv_via_writer(conn, sql), name)[-1]
    copied = _column(_dsv_via_copy(conn, sql), name)[-1]
    assert written == copied == "", f"{name}: NULL rendered {written!r} vs {copied!r}"


def test_quoting_agrees_on_the_awkward_strings(conn):
    """Commas, quotes and newlines inside values, checked as raw bytes.

    _column() parses with csv.reader, which would hide a quoting difference by
    normalising it away — two different encodings of the same value parse to the
    same cell. This compares the bytes.
    """
    sql = "SELECT 'a,b'::text AS c1, 'say \"hi\"'::text AS c2, E'l1\\nl2'::text AS c3"
    assert _dsv_via_writer(conn, sql) == _dsv_via_copy(conn, sql)


def test_the_divergence_list_is_exhaustive(conn):
    """Every divergence is either fixed or listed — nothing silently tolerated.

    The parametrised tests above cannot catch a *new* divergence in a kind
    already marked xfail. This walks every probe and asserts the set that
    actually differs is exactly the set claimed, so a coercion change that
    breaks a currently-passing kind is a failure here even if it were also
    marked expected elsewhere.
    """
    sql = _probe_sql(include_nulls=False)
    written_body, copied_body = _dsv_via_writer(conn, sql), _dsv_via_copy(conn, sql)
    differing = {
        name for name, _, _ in PROBES if _column(written_body, name) != _column(copied_body, name)
    }
    claimed = set(KNOWN_DIVERGENCES)
    assert differing == claimed, (
        f"newly diverging: {sorted(differing - claimed)}; "
        f"no longer diverging (remove from KNOWN_DIVERGENCES): {sorted(claimed - differing)}"
    )
