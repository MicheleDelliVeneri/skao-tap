"""The parts of the server-side DSV path that do not need a server.

Whether PostgreSQL renders what `csv.writer` renders is a question only
PostgreSQL can answer, and `tests/component/test_copy_dsv_differential.py` asks
it. What can be settled here is the accounting around it: that the block count
is the row count, that the row past MAXREC is read and not sent, that chunks
are batched rather than emitted per row, and that each shape the module declines
declines for the reason it claims.
"""

from __future__ import annotations

import types

import pytest
from egernia_core.query.copy_dsv import CopiedRows, Undecidable, header, projection
from egernia_core.query.results import ColumnMeta


def _description(*oids):
    return tuple(types.SimpleNamespace(name=f"c{i}", type_code=oid) for i, oid in enumerate(oids))


def _blocks(count: int, width: int = 8) -> list[bytes]:
    """`count` COPY blocks, one per row, as libpq delivers them."""
    return [(f"{i}," + "x" * width + "\n").encode() for i in range(count)]


# --- accounting -------------------------------------------------------------


@pytest.mark.parametrize(
    ("delivered", "maxrec", "count", "overflowed"),
    [(0, 10, 0, False), (1, 10, 1, False), (10, 10, 10, False), (11, 10, 10, True)],
)
def test_the_block_count_is_the_row_count(delivered, maxrec, count, overflowed):
    """`maxrec` exactly must not report OVERFLOW.

    A result that fills the limit is not truncated, and saying it was sends
    clients back to re-query something they already hold in full.
    """
    rows = CopiedRows(maxrec)
    body = b"".join(rows.chunks(iter(_blocks(delivered))))

    assert (rows.count, rows.overflowed) == (count, overflowed)
    assert body.count(b"\n") == count


def test_the_row_past_maxrec_is_read_and_dropped():
    """Read, because abandoning a COPY leaves the connection in COPY state for
    whoever borrows it next; dropped, because it is not part of the result."""
    consumed = []

    def blocks():
        for block in _blocks(5):
            consumed.append(block)
            yield block

    rows = CopiedRows(3)
    body = b"".join(rows.chunks(blocks()))

    assert len(consumed) == 5, "the generator was abandoned rather than drained"
    assert rows.count == 3
    assert body.count(b"\n") == 3


def test_blocks_are_batched_into_chunks():
    """One socket write per row would be its own kind of slow.

    libpq hands COPY output back a row at a time, so without batching this path
    would trade the writer's CPU for a syscall per row.
    """
    rows = CopiedRows(100_000)
    chunks = list(rows.chunks(iter(_blocks(20_000, width=200))))

    assert rows.count == 20_000
    assert len(chunks) < 200, f"{len(chunks)} chunks for 20,000 rows is not batching"
    assert all(len(chunk) >= CopiedRows.CHUNK_BYTES for chunk in chunks[:-1])


def test_a_newline_inside_a_value_does_not_inflate_the_count():
    """The reason this counts blocks and not newlines.

    A quoted text field can hold a raw newline — `csv.writer` and COPY both
    write one — so a count of newlines is an upper bound on rows, not the rows.
    """
    rows = CopiedRows(10)
    body = b"".join(rows.chunks(iter([b'1,"a\nb"\n', b'2,"c\nd"\n'])))

    assert rows.count == 2
    assert body.count(b"\n") == 4


def test_the_status_is_the_dali_spelling():
    assert CopiedRows(10).status == "OK"
    rows = CopiedRows(1)
    list(rows.chunks(iter(_blocks(2))))
    assert rows.status == "OVERFLOW"


# --- the projection ---------------------------------------------------------


def test_columns_are_renamed_positionally():
    """The expressions must never mention a name the query chose.

    A query may output a name needing quoting, or the same name twice; aliasing
    the subquery's columns positionally keeps both out of the generated SQL.
    """
    sql = projection(_description(701, 25), 'SELECT 1 AS "odd name", 2 AS x')

    assert "AS src(c0, c1)" in sql
    assert "odd name" not in sql.split("FROM (", 1)[0]


def test_an_unknown_type_declines_with_its_oids():
    """The reason has to name the types, or the next reader cannot act on it."""
    with pytest.raises(Undecidable) as raised:
        projection(_description(701, 17, 114), "SELECT 1")  # bytea, json

    assert raised.value.reason == "unknown_type"
    assert "17" in str(raised.value) and "114" in str(raised.value)


def test_the_delimiter_is_a_literal_not_interpolated_text():
    """Both DSV delimiters, and nothing else, reach the COPY statement."""
    from egernia_core.query.copy_dsv import _delimiter

    assert _delimiter(",") == "','"
    assert _delimiter("\t") == "E'\\t'"
    with pytest.raises(Undecidable) as raised:
        _delimiter(";")
    assert raised.value.reason == "delimiter"


def test_the_header_is_the_writers_header():
    """Written by `csv.writer`, so a name needing quoting is quoted the same
    way on both paths — and so COPY's own HEADER, which would emit the
    projection's invented aliases, is never used."""
    columns = [ColumnMeta("plain"), ColumnMeta("has,comma"), ColumnMeta('has"quote')]

    assert header(columns, ",") == b'plain,"has,comma","has""quote"\n'
    assert header(columns, "\t") == b'plain\thas,comma\t"has""quote"\n'


# --- the VOTable path -------------------------------------------------------


def test_the_votable_projection_is_one_row_per_output_column():
    from egernia_core.query.copy_dsv import votable_projection

    sql = votable_projection(_description(23, 25), "SELECT 1, 'x'")

    assert sql.startswith("SELECT '<TR>' || ")
    assert sql.count("coalesce('<TD>' || ") == 2, "one guarded cell per column"
    assert "'<TD/>'" in sql, "NULL has to spell itself the writer's way"
    assert sql.rstrip().endswith("AS src(c0, c1)")


def test_the_votable_projection_declines_unknown_types_like_dsv():
    from egernia_core.query.copy_dsv import votable_projection

    with pytest.raises(Undecidable) as raised:
        votable_projection(_description(25, 17), "SELECT 1")
    assert raised.value.reason == "unknown_type"


@pytest.mark.parametrize(
    ("escaped", "raw"),
    [
        (b"<TR><TD>plain</TD></TR>\n", b"<TR><TD>plain</TD></TR>\n"),
        (b"a\\\\b", b"a\\b"),
        (b"a\\tb\\nc\\rd", b"a\tb\nc\rd"),
        (b"\\b\\f\\v", b"\x08\x0c\x0b"),
        (b"\\\\n", b"\\n"),  # an escaped backslash followed by a literal n
    ],
)
def test_unescape_undoes_copy_text_escaping(escaped, raw):
    from egernia_core.query.copy_dsv import unescape

    assert unescape(escaped) == raw


def test_unescape_returns_a_chunk_with_no_escape_untouched():
    """The common ObsCore chunk: the cost has to be a memchr, not a copy."""
    from egernia_core.query.copy_dsv import unescape

    chunk = b"<TR><TD>ivo://skao.int/obs</TD><TD>1.5</TD></TR>\n" * 1000
    assert unescape(chunk) is chunk


def test_a_leading_job_tag_leads_the_copy_statement_too():
    """The executor's `/* tap_job_<id> */` prefix is what the abort watchdog
    looks for in `pg_stat_activity.query`, which is truncated at 1 KiB. Buried
    after the projection's float CASEs it would be cut off, so it is lifted
    to the front of the statement that runs -- for both formats, and for the
    LIMIT 0 probe."""
    from egernia_core.query.copy_dsv import _split_tag, stream_copy_dsv, stream_copy_votable

    assert _split_tag("/* tap_job_x */ SELECT 1") == ("/* tap_job_x */ ", "SELECT 1")
    assert _split_tag("SELECT 1 /* rid=abc */") == ("", "SELECT 1 /* rid=abc */")

    statements = []

    class _Cursor:
        def copy(self, statement):
            statements.append(statement)
            return _NoRows()

    columns = [ColumnMeta("a", kind="float64"), ColumnMeta("b", kind="str")]
    for start in (
        lambda: stream_copy_votable(
            _Cursor(), "/* tap_job_x */ SELECT 1", columns, _description(701, 25), 10
        ),
        lambda: stream_copy_dsv(
            _Cursor(), "/* tap_job_x */ SELECT 1", columns, _description(701, 25), ",", 10
        ),
    ):
        chunks, _rows = start()
        b"".join(chunks)
    assert [s.split("(", 1)[0] for s in statements] == [
        "/* tap_job_x */ COPY ",
        "/* tap_job_x */ COPY ",
    ]
    assert all("/* tap_job_x */ SELECT" not in s for s in statements), (
        "the tag was copied, not moved"
    )


class _NoRows:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(())
