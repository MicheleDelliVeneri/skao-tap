"""DSV and VOTable rows written by PostgreSQL instead of by CPython.

`stream_dsv` is the single busiest frame in the service: `_csv.writer`'s
quoting, plus psycopg materialising every row as a tuple of Python objects to
feed it — CPython's floor, which can only be moved, not shrunk. `COPY
(<query>) TO STDOUT WITH (FORMAT csv)` produces the bytes in the server and
hands them to the socket as buffers, so neither the tuples nor the writer
exist. In a deployment PostgreSQL is a different pod, so the split matters
more than the total: the writer's per-row cost leaves the API's CPU, and what
the database takes on in exchange is the projection. (Measurements:
docs/python-performance.md; reproduce with tests/component/test_copy_dsv_cost.py
and tests/benchmarks/test_hot_paths.py.)

The catch is that the two renderings are not the same bytes — several of the
divergences in float8, which is most of ObsCore — so this module does not use
raw `COPY`. It re-projects every column through an expression chosen to
reproduce what the Python path emits, and
`tests/component/test_copy_dsv_differential.py` is what says whether it does.
Formatting in the projection rather than afterwards is the whole point: a
per-row Python pass to repair the bytes would put back the cost being removed.

Any column this module cannot promise to reproduce — an unrecognised type, a
repeated column name, a single-column result — declines the whole result and the
caller falls back to `stream_dsv`. `COPY_DSV_FALLBACKS` counts that with the
reason, so a deployment can tell whether the fast path is the one it is actually
taking, and `TAP_COPY_DSV=false` turns the path off if it ever needs to be.

VOTable, TAP's default format, takes the same route: the projection folds each
row into one text column holding `<TR><TD>…</TD></TR>`, and `COPY ... FORMAT
text` streams it. The envelope — FIELDs before, the OVERFLOW INFO after — stays
in Python, where it costs one call per response rather than one per row. Text
format is the one COPY output that never quotes, but it does escape backslash,
tab, CR, LF and the three other C escapes, so each received chunk is passed
through `unescape` — a C-level scan that does nothing on the common chunk with
no backslash in it. `TAP_COPY_VOTABLE=false` and `COPY_VOTABLE_FALLBACKS` are
that path's switch and counter; `tests/component/test_copy_votable_differential.py`
holds its bytes to `stream_votable`'s.
"""

from __future__ import annotations

import contextlib
import csv
import io
import re
from collections.abc import Iterator

from prometheus_client import Counter

from ..config import settings
from ..db import StreamedRows
from ..observability import REGISTRY
from .results import RowLimiter, columns_from_cursor, stream, votable_head, votable_tail

COPY_DSV_FALLBACKS = Counter(
    "tap_copy_dsv_fallbacks_total",
    "DSV results served by the Python writer because the COPY path declined them",
    ["reason"],
    registry=REGISTRY,
)
COPY_DSV_RESULTS = Counter(
    "tap_copy_dsv_results_total",
    "DSV results served by the server-side COPY path",
    registry=REGISTRY,
)
COPY_VOTABLE_FALLBACKS = Counter(
    "tap_copy_votable_fallbacks_total",
    "VOTable results served by the Python writer because the COPY path declined them",
    ["reason"],
    registry=REGISTRY,
)
COPY_VOTABLE_RESULTS = Counter(
    "tap_copy_votable_results_total",
    "VOTable results served by the server-side COPY path",
    registry=REGISTRY,
)


# --- the projection ---------------------------------------------------------
#
# One expression per PostgreSQL type OID, rendering what the Python path
# renders. A column whose OID is absent declines the result: an expression
# that is merely probably right is worse than the writer, because its bytes
# reach clients unchecked.
#
# The integers and `date` need no expression at all — `COPY` already agrees
# with them byte for byte, and the cheapest formatting is none.

_PASSTHROUGH = frozenset(
    {
        20,  # int8
        21,  # int2
        23,  # int4
        1082,  # date
    }
)

_TEXT = (18, 19, 25, 1043)  # char, name, text, varchar


def _bpchar(text_expression):
    """char(n) through its output function, so the padding survives.

    psycopg hands the writer the padded value ('bp  ' for `'bp'::char(4)`),
    and COPY's own output of a bpchar keeps it too — but any text function
    or cast applied to a bpchar strips the trailing blanks first, and
    `''::bpchar` compares equal to blanks. `format('%s')` renders through the
    output function, and the text expression then sees what psycopg saw. It
    renders NULL as '' too, hence the guard: a NULL cell is `<TD/>`, not
    `<TD></TD>`.
    """
    return lambda col: text_expression(
        f"(CASE WHEN {col} IS NULL THEN NULL ELSE format('%s', {col}) END)"
    )


def _text_dsv(col: str) -> str:
    """Text with the empty string folded into NULL, as the writer sees it.

    COPY writes an empty string as `""` and a NULL as nothing, keeping them
    apart. `csv.writer` writes nothing for both — an empty field is an empty
    field — so the distinction does not survive the Python path and must not
    survive this one either. `nullif` is the cheapest way to lose it, and
    losing it is what byte equality means here.
    """
    return f"nullif({col}, '')"


def _text_votable(col: str) -> str:
    """`_xml_escape`: the three metacharacters, `&` first so it is not doubled.

    An empty string stays an empty string here — `<TD></TD>`, which is what the
    writer emits for it — where the DSV expression folds it into NULL.
    `replace` returns its input untouched when there is nothing to replace,
    so the three calls cost a scan each on ObsCore text, not a copy.
    """
    return f"replace(replace(replace({col}, '&', '&amp;'), '<', '&lt;'), '>', '&gt;')"


def _float(col: str) -> str:
    """A float8 rendered as CPython's `str` renders it.

    `::text` already agrees for most values: PostgreSQL 12 and later default
    `extra_float_digits` to shortest-round-trip, which is the same digits
    Python's repr produces. What differs is where each side stops writing
    those digits positionally and starts writing an exponent, and three
    special values.

    `to_char` is not among the options and neither is `::numeric` -- both
    round to 15 significant digits (`1234567890123456.0::float8::numeric` is
    `1234567890123460`), so neither can carry a shortest-round-trip value at
    all. That is why this is a CASE over `::text` rather than a format.

    NaN compares equal to itself in PostgreSQL, which is what makes the first
    branch reachable.
    """
    return f"""CASE
        WHEN {col} IS NULL THEN NULL
        WHEN {col} = 'NaN'::float8 THEN 'nan'
        WHEN {col} = 'Infinity'::float8 THEN 'inf'
        WHEN {col} = '-Infinity'::float8 THEN '-inf'
        WHEN abs({col}) >= 1e15 AND abs({col}) < 1e16
            THEN trunc({col})::int8::text
                 || '.' || round(abs({col} - trunc({col})) * 10)::int8::text
        WHEN {col} = trunc({col}) AND abs({col}) < 1e15 THEN {col}::text || '.0'
        ELSE {col}::text
    END"""


def _float4(col: str) -> str:
    """float4 through its own text, because that is what psycopg parsed.

    psycopg reads float4 in text mode and hands back `float(that text)` -- a
    double built from float4's shortest representation, not the widened
    single. So `0.1::float4` reaches the writer as 0.1 and prints '0.1',
    where `0.1::float4::float8` is 0.10000000149011612. The text round-trip
    reproduces the client's value exactly; the numeric widening does not.
    """
    return _float(f"({col}::text::float8)")


def _numeric(col: str) -> str:
    """numeric via float8, because `_VALUE_COERCIONS` maps decimal to float.

    This degrades the value — numeric holds more than a double — but it
    degrades it exactly as the Python path already does, which is what byte
    equality means here. A path that kept numeric's precision would be a
    different response, not a faster one.
    """
    return _float(f"({col}::float8)")


def _fraction(col: str) -> str:
    """`.ffffff` when the microseconds are not zero, else nothing.

    Python emits six fractional digits or none at all, never a trailing zero
    group; PostgreSQL's `::text` trims trailing zeros ('03:04:05.5'), so
    neither `time` nor `timestamp` can be passed through.
    """
    return f"""CASE WHEN to_char({col}, 'US') = '000000'
            THEN '' ELSE '.' || to_char({col}, 'US') END"""


def _time(col: str) -> str:
    """`time.isoformat()`: HH:MM:SS, then the fraction as Python prints it."""
    return f"""CASE WHEN {col} IS NULL THEN NULL ELSE
        to_char({col}, 'HH24:MI:SS') || {_fraction(col)}
    END"""


def _timestamp(col: str, offset: str = "") -> str:
    """`datetime.isoformat()`: 'T' between date and time, fraction only if any.

    PostgreSQL's own text output uses a space, so this cannot be `::text`.
    """
    return f"""CASE WHEN {col} IS NULL THEN NULL ELSE
        to_char({col}, 'YYYY-MM-DD"T"HH24:MI:SS') || {_fraction(col)}{offset}
    END"""


def _timestamptz(col: str) -> str:
    """As `_timestamp`, converted to UTC with the offset dropped.

    DALI timestamps are UTC by definition and carry no zone suffix; the
    Python writer converts aware datetimes the same way, and the
    differential test holds the two renderings byte-identical.
    """
    return _timestamp(f"({col} AT TIME ZONE 'UTC')")


def _bool(true: str, false: str):
    # PostgreSQL's own 't'/'f' is a third spelling again: `csv.writer` calls
    # `str` on the Python bool ('True'), VOTable spells it in lower case. The
    # IS NULL branch is not decoration -- CASE WHEN col THEN ... ELSE renders
    # NULL as 'False', turning absent data into data.
    return lambda col: (
        f"CASE WHEN {col} IS NULL THEN NULL WHEN {col} THEN '{true}' ELSE '{false}' END"
    )


# The two writers agree on every number and every timestamp -- both call `str`
# or `isoformat` -- and differ only on text and bool.
_SHARED = {
    700: _float4,
    701: _float,
    1700: _numeric,
    1083: _time,
    1114: _timestamp,
    1184: _timestamptz,
}
_EXPRESSIONS = {
    **_SHARED,
    **dict.fromkeys(_TEXT, _text_dsv),
    1042: _bpchar(_text_dsv),
    16: _bool("True", "False"),
}
_VOTABLE_EXPRESSIONS = {
    **_SHARED,
    **dict.fromkeys(_TEXT, _text_votable),
    1042: _bpchar(_text_votable),
    16: _bool("true", "false"),
}


class Undecidable(Exception):
    """This result cannot be promised byte-identical; use the Python writer.

    Carries the reason as its `reason` attribute, which is the Prometheus
    label, so the counter says *why* a deployment is on the slow path rather
    than only that it is.
    """

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason


def _cells(description, expressions: dict) -> tuple[list[str], list[str]]:
    """(aliases, one expression per column), or `Undecidable` for a type with
    no expression."""
    unknown = sorted({e.type_code for e in description} - _PASSTHROUGH - set(expressions))
    if unknown:
        raise Undecidable("unknown_type", f"no COPY rendering for type OIDs {unknown}")
    aliases = [f"c{i}" for i in range(len(description))]
    cells = [
        expressions[entry.type_code](alias) if entry.type_code in expressions else alias
        for entry, alias in zip(description, aliases, strict=True)
    ]
    return aliases, cells


def _from(inner_sql: str, aliases: list[str]) -> str:
    return f"FROM ({inner_sql.rstrip().rstrip(';')}) AS src({', '.join(aliases)})"


def projection(description, inner_sql: str) -> str:
    """`inner_sql` re-projected so `COPY ... FORMAT csv` matches `stream_dsv`.

    Columns are renamed positionally by the alias list, so the expressions
    never mention a name the query chose. That is not cosmetic: a query may
    output the same name twice, or a name needing quoting, and neither can
    reach the generated SQL this way.

    Raises `Undecidable` for a type with no expression.
    """
    aliases, cells = _cells(description, _EXPRESSIONS)
    return f"SELECT {', '.join(cells)} {_from(inner_sql, aliases)}"


def votable_projection(description, inner_sql: str) -> str:
    """`inner_sql` folded into one text column per row: `stream_votable`'s `<TR>`.

    Every cell is `<TD>text</TD>` or, for NULL, `<TD/>` -- `coalesce` over the
    concatenation is what turns a NULL cell into the second spelling without
    a CASE per cell. The row is never NULL itself, so COPY's NULL marker never
    appears; the newline after `</TR>` is COPY's own row terminator.
    """
    aliases, cells = _cells(description, _VOTABLE_EXPRESSIONS)
    tds = " || ".join(f"coalesce('<TD>' || ({cell})::text || '</TD>', '<TD/>')" for cell in cells)
    return f"SELECT '<TR>' || {tds} || '</TR>' {_from(inner_sql, aliases)}"


# The executor prefixes its statement with `/* tap_job_<id> */` so the abort
# watchdog can find it in `pg_stat_activity.query`, which is truncated at
# track_activity_query_size (1 KiB by default). Wrapped inside a projection
# whose float CASEs alone run past that, the tag would be cut off; so a
# leading comment is lifted out of the inner query and put in front of the
# statement that actually runs.
_LEADING_COMMENT = re.compile(r"\s*(/\*.*?\*/)\s*", re.DOTALL)


def _split_tag(sql: str) -> tuple[str, str]:
    """(leading comment with a trailing space, or ''; the rest of `sql`)."""
    match = _LEADING_COMMENT.match(sql)
    if match is None:
        return "", sql
    return match.group(1) + " ", sql[match.end() :]


# --- the stream -------------------------------------------------------------


class CopiedRows:
    """`RowLimiter`'s count and overflow, for a result delivered as bytes.

    `apply_maxrec` asks for `maxrec + 1` rows so that the extra one proves
    DALI overflow. The Python path sees rows and stops; this path sees the
    bytes PostgreSQL wrote, so the extra row has to be kept out of the body
    without a per-row Python pass reappearing to find it.

    It does not need one. libpq hands back COPY output one row per block --
    `PQgetCopyData` is defined that way, and psycopg passes the blocks through
    -- so the block count *is* the row count, exactly, with no scanning and no
    guessing about newlines inside quoted values. Blocks are coalesced into
    64 KiB chunks before they leave, which is the same batching `stream_dsv`
    does and for the same reason: one socket write per row is not a saving.

    The extra row is read and dropped rather than abandoned mid-COPY. Reading
    one more small row costs nothing, where walking away from an unfinished
    COPY would hand a connection back to the pool still in COPY state.

    `count` and `overflowed` are final once the stream is exhausted, which is
    when DSV consumers read them: a DSV body, unlike VOTable or JSON, has
    nowhere to carry a status, so nothing needs them mid-stream.
    """

    CHUNK_BYTES = 65536

    def __init__(self, maxrec: int):
        self._maxrec = maxrec
        self.count = 0
        self.overflowed = False

    @property
    def status(self) -> str:
        return "OVERFLOW" if self.overflowed else "OK"

    def chunks(self, copy) -> Iterator[bytes]:
        out = bytearray()
        delivered = 0
        for block in copy:
            delivered += 1
            if delivered > self._maxrec:
                self.overflowed = True
                continue  # read past the limit, but never send it
            out += block
            if len(out) >= self.CHUNK_BYTES:
                yield bytes(out)
                out.clear()
        self.count = min(delivered, self._maxrec)
        if out:
            yield bytes(out)


def header(columns, delimiter: str) -> bytes:
    """The header row, written by the `csv.writer` the fallback body uses.

    `COPY ... HEADER` would write it too, but out of the aliases the projection
    invented, and quoting names is a second place for the two paths to
    disagree. One writer call per response is not a cost worth avoiding.
    """
    out = io.StringIO()
    csv.writer(out, delimiter=delimiter, lineterminator="\n").writerow([c.name for c in columns])
    return out.getvalue().encode()


def stream_copy_dsv(cur, inner_sql: str, columns, description, delimiter: str, maxrec: int):
    """(chunk iterator, row accounting) for the server-side DSV path.

    Raises `Undecidable` before yielding anything, so the caller can still
    choose the Python writer — the query has not been executed at that point.
    """
    if len(columns) == 1:
        # `csv.writer` writes a lone NULL field as `""` rather than as nothing,
        # so the line is not empty; COPY writes nothing, and its CSV quoting
        # cannot be talked into the other spelling -- a NULL string containing
        # the quote character is rejected outright. One column is the only
        # shape where that shows, so it is the only shape declined.
        raise Undecidable("single_column", "a lone NULL renders differently in csv.writer")
    if len({c.name for c in columns}) != len(columns):
        raise Undecidable("duplicate_column", "the result has repeated column names")
    tag, body = _split_tag(inner_sql)
    projected = projection(description, body)
    rows = CopiedRows(maxrec)

    def chunks() -> Iterator[bytes]:
        yield header(columns, delimiter)
        statement = (
            f"{tag}COPY ({projected}) TO STDOUT"
            f" WITH (FORMAT csv, DELIMITER {_delimiter(delimiter)})"
        )
        with cur.copy(statement) as copy:
            yield from rows.chunks(copy)
        COPY_DSV_RESULTS.inc()

    return chunks(), rows


# COPY's text format writes backslash, the delimiter (tab) and the C escapes
# `\b \f \n \r \t \v` as two-character escapes and everything else raw; those
# seven are its whole output alphabet after a backslash, so the table is closed.
_ESCAPES = {
    b"\\": b"\\",
    b"b": b"\b",
    b"f": b"\f",
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"v": b"\v",
}
_ESCAPE = re.compile(rb"\\(.)")


def unescape(chunk: bytes) -> bytes:
    """A COPY text chunk as the raw bytes it stands for.

    The `in` test is a C memchr and is the whole cost on ObsCore text, which
    holds none of the escaped bytes. No escape straddles two chunks: a chunk
    is whole rows -- `CopiedRows` coalesces libpq's one-row blocks -- and a row
    ends in the literal newline COPY terminates it with, never a backslash.
    """
    if b"\\" not in chunk:
        return chunk
    return _ESCAPE.sub(lambda match: _ESCAPES[match.group(1)], chunk)


def stream_copy_votable(cur, inner_sql: str, columns, description, maxrec: int):
    """(chunk iterator, row accounting) for the server-side VOTable path.

    Neither DSV decline applies. A lone NULL is `<TR><TD/></TR>` on both paths,
    and repeated names are no worse than on the writer's path: the FIELDs are
    written from the same cursor description either way, and the projection
    renders positionally. Raises `Undecidable` (an unknown type) before
    yielding anything, so the caller can still choose the Python writer.
    """
    tag, body = _split_tag(inner_sql)
    projected = votable_projection(description, body)
    rows = CopiedRows(maxrec)

    def chunks() -> Iterator[bytes]:
        yield votable_head(columns)
        with cur.copy(f"{tag}COPY ({projected}) TO STDOUT WITH (FORMAT text)") as copy:
            for chunk in rows.chunks(copy):
                yield unescape(chunk)
        yield votable_tail(rows.overflowed)
        COPY_VOTABLE_RESULTS.inc()

    return chunks(), rows


def _delimiter(delimiter: str) -> str:
    """The delimiter as a SQL literal. Only DSV's two reach here."""
    if delimiter == "\t":
        return "E'\\t'"
    if delimiter == ",":
        return "','"
    raise Undecidable("delimiter", f"no COPY literal for delimiter {delimiter!r}")


# --- choosing between the two paths -----------------------------------------

_DELIMITERS = {"csv": ",", "tsv": "\t"}


def _describe(cur, sql: str, tap_meta: dict):
    """The result's columns, without producing a row of it.

    `LIMIT 0` parses and plans the query but never pulls a tuple from its
    child, so the query's work is not paid twice — the same probe
    `StreamedRows` uses to recover the columns of an empty result. It is the
    price of this path: the COPY projection has to know the column types
    before the query runs, and only the server can say what they are.
    """
    tag, body = _split_tag(sql.rstrip().rstrip(";"))
    cur.execute(f"{tag}SELECT * FROM ({body}) AS probe LIMIT 0")
    return columns_from_cursor(cur.description, tap_meta), cur.description


@contextlib.contextmanager
def result_stream(cur, sql: str, tap_meta: dict, fmt_key: str, maxrec: int, chunk_rows: int):
    """(chunks, row accounting) for `sql`, server-side when the format allows it.

    Yields the same pair either way — an iterator of body bytes, and an object
    carrying `count`, `overflowed` and `status` — so callers do not branch on
    which path they got. The Python writer is the fallback and stays the
    reference implementation: whatever the COPY path cannot promise to
    reproduce byte for byte, it declines, and the decline is counted.

    A context manager because the fallback holds a streaming cursor that has
    to be closed even if the consumer walks away mid-download; the COPY path
    has nothing to close but is yielded the same way.
    """
    server = fallbacks = None
    if fmt_key in _DELIMITERS and settings.copy_dsv:
        server, fallbacks = (
            lambda c, d: stream_copy_dsv(cur, sql, c, d, _DELIMITERS[fmt_key], maxrec),
            COPY_DSV_FALLBACKS,
        )
    elif fmt_key == "votable" and settings.copy_votable:
        server, fallbacks = (
            lambda c, d: stream_copy_votable(cur, sql, c, d, maxrec),
            COPY_VOTABLE_FALLBACKS,
        )
    if server is not None:
        try:
            columns, description = _describe(cur, sql, tap_meta)
            chunks, rows = server(columns, description)
        except Undecidable as exc:
            fallbacks.labels(reason=exc.reason).inc()
        else:
            yield chunks, rows
            return

    streamed = StreamedRows(cur, sql, chunk_rows=chunk_rows)
    with contextlib.closing(streamed):
        columns = columns_from_cursor(cur.description, tap_meta)
        limiter = RowLimiter(streamed, maxrec)
        yield stream(columns, limiter, fmt_key), limiter
