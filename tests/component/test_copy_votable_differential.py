"""Does the VOTable COPY projection produce the bytes `stream_votable` produces?

The DSV differential (`test_copy_dsv_differential.py`) is the model, and its
probes are reused: every column kind the projection accepts, at boundary values
and at NULL. What is different here is the container. The rows come back from
`COPY ... FORMAT text` as one escaped column, so the questions this file adds
are the ones that format raises — that a backslash, a tab, CR and LF inside a
cell are un-escaped back to the bytes the writer emits, that `&`, `<` and `>`
are escaped once and in the writer's order, that the empty string is
`<TD></TD>` and NULL is `<TD/>`, and that char(n) keeps its padding, which
every PostgreSQL text function strips.

Compared as whole bodies rather than parsed cells: an XML parser would
normalise exactly the whitespace and escaping differences this file exists to
catch.
"""

from __future__ import annotations

import psycopg
import pytest
from egernia_core.query.copy_dsv import (
    COPY_VOTABLE_RESULTS,
    Undecidable,
    result_stream,
    unescape,
    votable_projection,
)
from egernia_core.query.results import RowLimiter, columns_from_cursor, stream_votable

from tests.component.test_copy_dsv_differential import PROBES

pytestmark = pytest.mark.component

# The DSV probes, plus what only VOTable's rendering can get wrong.
VOTABLE_PROBES = [
    *PROBES,
    ("text_metachars", "text", "'a&b<c>d'::text"),
    ("text_amp_entity", "text", "'&amp;'::text"),  # escaped once, not twice
    ("text_backslash", "text", r"E'a\\b'::text"),
    ("text_trailing_backslash", "text", r"E'a\\'::text"),
    ("text_control", "text", r"E'a\bb\fc\vd'::text"),
    ("text_unicode", "text", "'ångström 🔭'::text"),
    ("time_half", "time", "'03:04:05.5'::time"),
    ("bpchar_blank", "bpchar", "'    '::char(4)"),
    ("bpchar_padded_metachar", "bpchar", "'<'::char(3)"),
]


def _probe_sql(include_nulls: bool) -> str:
    cells = ", ".join(f"{expr} AS {name}" for name, _, expr in VOTABLE_PROBES)
    if not include_nulls:
        return f"SELECT {cells}"
    nulls = ", ".join(f"NULL::{sql_type} AS {name}" for name, sql_type, _ in VOTABLE_PROBES)
    return f"SELECT {cells} UNION ALL SELECT {nulls}"


def _via_writer(conn, sql: str, maxrec: int = 1000) -> bytes:
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = columns_from_cursor(cur.description, {})
        return b"".join(stream_votable(columns, RowLimiter(cur.fetchall(), maxrec)))


def _via_copy(conn, sql: str, maxrec: int = 1000) -> tuple[bytes, object]:
    """Through `result_stream`, the entry point the services use, so the
    un-escape, the envelope and the row accounting are all in the comparison."""
    before = COPY_VOTABLE_RESULTS._value.get()
    with (
        conn.cursor() as cur,
        result_stream(cur, sql, {}, "votable", maxrec, 100) as (chunks, rows),
    ):
        body = b"".join(chunks)
    assert COPY_VOTABLE_RESULTS._value.get() == before + 1, "the writer served this, not COPY"
    return body, rows


def _rows(body: bytes) -> list[bytes]:
    return body.split(b"<TABLEDATA>\n", 1)[1].split(b"</TABLEDATA>", 1)[0].split(b"</TR>\n")[:-1]


def _cell(row: bytes, index: int) -> bytes:
    """One `<TD…` of a `<TR>` row, so a failure names the probe."""
    cells, pos = [], 4  # past '<TR>'
    while pos < len(row):
        end = row.index(b"<TD", pos + 3) if row.count(b"<TD", pos + 3) else len(row)
        cells.append(row[pos:end])
        pos = end
    return cells[index]


@pytest.fixture(scope="module")
def conn(database_url):
    with psycopg.connect(database_url, autocommit=True) as connection:
        yield connection


@pytest.mark.parametrize("name", [p[0] for p in VOTABLE_PROBES])
def test_each_column_kind_renders_identically(conn, name):
    sql = _probe_sql(include_nulls=False)
    index = [p[0] for p in VOTABLE_PROBES].index(name)
    written = _cell(_rows(_via_writer(conn, sql))[0], index)
    copied = _cell(_rows(_via_copy(conn, sql)[0])[0], index)
    assert written == copied, f"{name}: stream_votable wrote {written!r}, COPY wrote {copied!r}"


@pytest.mark.parametrize("name", [p[0] for p in VOTABLE_PROBES])
def test_null_renders_identically(conn, name):
    """`<TD/>` and never `<TD></TD>`: `format('%s')` and `coalesce` both have a
    spelling for NULL, and only the writer's is right."""
    sql = _probe_sql(include_nulls=True)
    index = [p[0] for p in VOTABLE_PROBES].index(name)
    written = _cell(_rows(_via_writer(conn, sql))[1], index)
    copied = _cell(_rows(_via_copy(conn, sql)[0])[1], index)
    assert written == copied == b"<TD/>", f"{name}: NULL rendered {written!r} vs {copied!r}"


def test_the_whole_body_is_identical(conn):
    """Envelope, rows and NULLs together, as bytes."""
    sql = _probe_sql(include_nulls=True)
    assert _via_copy(conn, sql)[0] == _via_writer(conn, sql)


def test_a_single_column_is_not_declined(conn):
    """DSV declines this shape because `csv.writer` quotes a lone NULL; VOTable
    has no such spelling, so the shape is served."""
    sql = "SELECT v FROM (VALUES (NULL::float8), (1.5)) AS t(v)"
    body, _rows = _via_copy(conn, sql)
    assert body == _via_writer(conn, sql)
    assert b"<TR><TD/></TR>\n" in body


def test_repeated_column_names_are_not_declined(conn):
    """FIELDs come from the same cursor description on both paths, and the
    projection renders positionally, so nothing about the repetition differs."""
    sql = "SELECT 1::int4 AS a, 'x'::text AS a"
    assert _via_copy(conn, sql)[0] == _via_writer(conn, sql)


def test_an_unknown_type_is_declined(conn):
    sql = "SELECT '\\x0001'::bytea AS b"
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM ({sql}) AS probe LIMIT 0")
        with pytest.raises(Undecidable) as raised:
            votable_projection(cur.description, sql)
    assert raised.value.reason == "unknown_type"
    # and the writer serves it, so the client never sees the decline
    with conn.cursor() as cur, result_stream(cur, sql, {}, "votable", 10, 100) as (chunks, _):
        body = b"".join(chunks)
    assert body == _via_writer(conn, sql)


@pytest.mark.parametrize(("total", "maxrec"), [(0, 10), (9, 10), (10, 10), (11, 10)])
def test_overflow_is_reported_in_the_tail_as_the_writer_reports_it(conn, total, maxrec):
    """The OVERFLOW INFO is written after the rows, from the block count."""
    sql = (
        f"SELECT * FROM (SELECT i::int4 AS n, 'r' || i AS t"
        f" FROM generate_series(1, {total}) AS i) AS s LIMIT {maxrec + 1}"
    )
    body, rows = _via_copy(conn, sql, maxrec)
    assert body == _via_writer(conn, sql, maxrec)
    assert (rows.count, rows.overflowed) == (min(total, maxrec), total > maxrec)
    assert (b'value="OVERFLOW"' in body) == (total > maxrec)


def test_escapes_survive_chunk_boundaries(conn):
    """Rows with escapes in every cell, across many 64 KiB chunks.

    A trailing backslash split between two chunks would un-escape wrongly; the
    chunks are whole rows, so it cannot happen, and this is the demonstration.
    """
    sql = (
        r"SELECT E'\\' || repeat('x', 100) || E'\\' AS a, E'\t\r\n' AS b, '<' AS c"
        " FROM generate_series(1, 5000)"
    )
    body, rows = _via_copy(conn, sql, 10_000)
    assert rows.count == 5000
    assert body == _via_writer(conn, sql, 10_000)


def test_unescape_is_the_inverse_of_copy_text_escaping(conn):
    """Every byte COPY's text format escapes, as one raw COPY row."""
    raw = b"a\\b\tc\nd\re\x08f\x0cg\x0bh"
    with (
        conn.cursor() as cur,
        cur.copy(r"COPY (SELECT E'a\\b\tc\nd\re\bf\fg\vh') TO STDOUT") as copy,
    ):
        block = b"".join(bytes(chunk) for chunk in copy)
    assert block != raw + b"\n", "COPY text did not escape anything: the un-escape is untested"
    assert unescape(block) == raw + b"\n"
