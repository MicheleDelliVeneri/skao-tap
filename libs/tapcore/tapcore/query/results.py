"""Typed, streaming result serialization.

Columns are typed from the PostgreSQL cursor description (type OIDs) and
enriched with unit/UCD/description drawn from TAP_SCHEMA.columns for the
tables the query touches. Serializers are generators of byte chunks fed
from a server-side cursor, so result sets are never fully materialized:
VOTable (TABLEDATA), CSV, TSV, JSON, Parquet, and an Arrow IPC stream.

DALI overflow is detected by fetching one row past MAXREC and reported in
the formats that can carry a status: a trailing INFO in VOTable, the JSON
status field, and Parquet file metadata. The Arrow IPC *stream* format has
no end-of-stream metadata slot, so it carries per-field metadata only.

Typing is what makes a large result cheap. A column's kind names the Python
type psycopg produces as well as the wire type the writers emit, so the type
dispatch a cell needs is answered once per *column* — when the cursor
description is read — instead of once per cell. A ten-thousand-row ObsCore
result is 110,000 cells, and the difference between deciding per cell and
deciding per column is most of what such a response costs. The kinds that
need no conversion at all reach the writers untouched; the ones that do
(`decimal`, `timestamp`, `opaque`) carry a coercion, applied only to their
own columns.

`opaque` is the honest name for a column whose type is not one this module
knows: an unrecognised OID, or a caller who built ColumnMeta without saying.
Those keep the fully dynamic per-cell path, which is what they need.
"""

import csv
import datetime
import io
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from xml.sax.saxutils import quoteattr

# canonical kind -> (VOTable datatype, VOTable arraysize, VOTable xtype)
_VOT_TYPES = {
    "bool": ("boolean", None, None),
    "int16": ("short", None, None),
    "int32": ("int", None, None),
    "int64": ("long", None, None),
    "float32": ("float", None, None),
    "float64": ("double", None, None),
    "decimal": ("double", None, None),
    "str": ("char", "*", None),
    "opaque": ("char", "*", None),
    "timestamp": ("char", "*", "timestamp"),
}

# PostgreSQL type OID -> canonical kind. The text family is listed explicitly
# rather than falling through to the default: psycopg guarantees `str` for
# these, which is what lets the writers skip conversion, and an OID that is
# genuinely unknown must not inherit that guarantee.
_OID_KINDS = {
    16: "bool",  # bool
    18: "str",  # char
    19: "str",  # name
    21: "int16",  # int2
    23: "int32",  # int4
    20: "int64",  # int8
    25: "str",  # text
    700: "float32",  # float4
    701: "float64",  # float8
    1042: "str",  # bpchar
    1043: "str",  # varchar
    1700: "decimal",  # numeric -> Decimal, emitted as a double
    1082: "timestamp",  # date
    1083: "timestamp",  # time
    1114: "timestamp",  # timestamp
    1184: "timestamp",  # timestamptz
}


@dataclass
class ColumnMeta:
    name: str
    # Untyped by default: a caller who does not say what a column holds gets
    # the dynamic path rather than a promise this module cannot keep.
    kind: str = "opaque"
    unit: str | None = None
    ucd: str | None = None
    description: str | None = None

    @property
    def vot_type(self) -> tuple[str, str | None, str | None]:
        return _VOT_TYPES[self.kind]


def tap_schema_metadata(conn, tables: Iterable[str]) -> dict[str, dict]:
    """unit/ucd/description per column name across the touched tables.

    A name published by several touched tables with conflicting annotations
    is dropped (joins make the mapping ambiguous).
    """
    names = list(tables)
    if not names:
        return {}
    rows = conn.execute(
        "SELECT column_name, unit, ucd, description FROM tap_schema.columns"
        " WHERE lower(table_name) = ANY(%s)",
        ([n.lower() for n in names],),
    ).fetchall()
    meta: dict[str, dict] = {}
    conflicted: set[str] = set()
    for name, unit, ucd, description in rows:
        entry = {"unit": unit, "ucd": ucd, "description": description}
        if name in conflicted:
            continue
        if name in meta and meta[name] != entry:
            meta[name] = {"unit": None, "ucd": None, "description": None}
            conflicted.add(name)
        else:
            meta.setdefault(name, entry)
    return meta


def columns_from_cursor(description, tap_meta: dict[str, dict]) -> list[ColumnMeta]:
    columns = []
    for entry in description:
        extra = tap_meta.get(entry.name, {})
        columns.append(
            ColumnMeta(
                name=entry.name,
                kind=_OID_KINDS.get(entry.type_code, "opaque"),
                unit=extra.get("unit"),
                ucd=extra.get("ucd"),
                description=extra.get("description"),
            )
        )
    return columns


class RowLimiter:
    """Yields at most ``maxrec`` rows; remembers whether more were available
    (DALI overflow) and how many rows were emitted."""

    def __init__(self, rows: Iterable[tuple], maxrec: int):
        self._rows = iter(rows)
        self._maxrec = maxrec
        self.overflowed = False
        self.count = 0

    def __iter__(self) -> Iterator[tuple]:
        for row in self._rows:
            if self.count >= self._maxrec:
                self.overflowed = True
                return
            self.count += 1
            yield row

    @property
    def status(self) -> str:
        return "OVERFLOW" if self.overflowed else "OK"


def _plain(value):
    """Convert a value of unknown type to a JSON/CSV-friendly primitive.

    Only `opaque` columns reach this: for every kind this module recognises,
    the conversion (or the absence of one) is decided per column instead.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _plain_text(value) -> str:
    """`_plain`, forced to text — for writers with a declared string column."""
    return str(_plain(value))


def _isoformat(value) -> str:
    return value.isoformat()


def _xml_escape(text: str) -> str:
    """`xml.sax.saxutils.escape` for text that usually needs no escaping.

    saxutils always runs three `str.replace` calls, each of which allocates a
    new string whether or not it changed anything — three allocations per cell
    on a result whose text is ObsCore identifiers and URLs, which contain none
    of the three characters. Testing first is a scan without an allocation.
    """
    if "&" in text or "<" in text or ">" in text:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text


def _vot_bool(value) -> str:
    return "true" if value else "false"


def _vot_decimal(value) -> str:
    return str(float(value))


def _vot_opaque(value) -> str:
    # bool before anything else: a bool is an int in Python, and VOTable
    # spells the literals in lower case. A column declared `bool` never
    # reaches here, but an undeclared one holding a bool must still be
    # written as VOTable and not as Python.
    if value is True:
        return "true"
    if value is False:
        return "false"
    return _xml_escape(str(_plain(value)))


# kind -> the cell coercion the value-oriented writers need. A kind absent
# here needs none: psycopg already produced the value CSV, JSON and Arrow
# want, and the cheapest conversion is the one that is not performed.
_VALUE_COERCIONS = {
    "decimal": float,
    "timestamp": _isoformat,
    "opaque": _plain,
}

# Arrow and Parquet declare `opaque` as a string column, so its value has to
# arrive as text rather than as whatever `_plain` made of it.
_ARROW_COERCIONS = {**_VALUE_COERCIONS, "opaque": _plain_text}

# kind -> the cell's VOTable TABLEDATA text. `str` for the numeric kinds is
# the builtin: a C call, and no escaping, because no decimal representation
# of a number contains an XML metacharacter. ISO-8601 timestamps do not
# either, so they are not escaped after formatting.
_VOT_TEXT = {
    "bool": _vot_bool,
    "int16": str,
    "int32": str,
    "int64": str,
    "float32": str,
    "float64": str,
    "decimal": _vot_decimal,
    "timestamp": _isoformat,
    "str": _xml_escape,
    "opaque": _vot_opaque,
}


def _coercion_plan(columns: list[ColumnMeta], table: dict) -> list[tuple[int, object]]:
    """(index, coercion) for the columns of `columns` that need one."""
    return [(i, fn) for i, c in enumerate(columns) if (fn := table.get(c.kind)) is not None]


def _coerced(row, plan: list[tuple[int, object]]) -> list:
    """`row` as a list, with the planned coercions applied. NULL stays NULL."""
    cells = list(row)
    for index, coerce in plan:
        value = cells[index]
        if value is not None:
            cells[index] = coerce(value)
    return cells


def _coerced_rows(rows: Iterable[tuple], plan: list[tuple[int, object]]) -> Iterable:
    """`rows`, coerced — or `rows` itself when nothing needs coercing.

    Decided once for the result set rather than tested per row, so a fully
    typed result set reaches the writer with no Python executed per cell.
    """
    if not plan:
        return rows
    return (_coerced(row, plan) for row in rows)


def stream_votable(columns: list[ColumnMeta], rows: RowLimiter) -> Iterator[bytes]:
    head = [
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">\n'
        '<RESOURCE type="results">\n'
        '<INFO name="QUERY_STATUS" value="OK"/>\n'
        "<TABLE>\n"
    ]
    for col in columns:
        datatype, arraysize, xtype = col.vot_type
        attrs = [f"name={quoteattr(col.name)}", f'datatype="{datatype}"']
        if arraysize:
            attrs.append(f'arraysize="{arraysize}"')
        if xtype:
            attrs.append(f'xtype="{xtype}"')
        if col.unit:
            attrs.append(f"unit={quoteattr(col.unit)}")
        if col.ucd:
            attrs.append(f"ucd={quoteattr(col.ucd)}")
        if col.description:
            head.append(
                f"<FIELD {' '.join(attrs)}>"
                f"<DESCRIPTION>{_xml_escape(col.description)}</DESCRIPTION></FIELD>\n"
            )
        else:
            head.append(f"<FIELD {' '.join(attrs)}/>\n")
    head.append("<DATA><TABLEDATA>\n")
    yield "".join(head).encode()

    # One text encoder per column, chosen from its kind. The row loop below
    # then has no type dispatch left to do: it decides only whether the row
    # contains a NULL, because `<TD/>` is the one cell whose markup differs.
    encoders = [_VOT_TEXT.get(col.kind, _vot_opaque) for col in columns]

    buffer: list[str] = []
    for row in rows:
        if None in row:
            cells = "".join(
                "<TD/>" if value is None else f"<TD>{encode(value)}</TD>"
                for encode, value in zip(encoders, row, strict=True)
            )
        else:
            # No NULL, so every cell has the same markup and the whole row is
            # two C-level calls: encode each cell, then one join.
            cells = (
                "<TD>"
                + "</TD><TD>".join(
                    [encode(value) for encode, value in zip(encoders, row, strict=True)]
                )
                + "</TD>"
            )
        buffer.append(f"<TR>{cells}</TR>\n")
        if len(buffer) >= 500:
            yield "".join(buffer).encode()
            buffer.clear()
    if buffer:
        yield "".join(buffer).encode()

    tail = "</TABLEDATA></DATA>\n</TABLE>\n"
    if rows.overflowed:  # DALI: overflow indicator after the table
        tail += '<INFO name="QUERY_STATUS" value="OVERFLOW"/>\n'
    tail += "</RESOURCE>\n</VOTABLE>\n"
    yield tail.encode()


def stream_dsv(columns: list[ColumnMeta], rows: RowLimiter, delimiter: str) -> Iterator[bytes]:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
    writer.writerow([c.name for c in columns])
    # `csv.writer` already renders None as an empty field and calls `str` on
    # everything else in C, so a fully typed row goes straight to it: the
    # per-cell list comprehension this used to build was the whole cost.
    for row in _coerced_rows(rows, _coercion_plan(columns, _VALUE_COERCIONS)):
        writer.writerow(row)
        if out.tell() >= 65536:
            yield out.getvalue().encode()
            out.seek(0)
            out.truncate()
    yield out.getvalue().encode()


def stream_json(columns: list[ColumnMeta], rows: RowLimiter) -> Iterator[bytes]:
    metadata = [
        {
            "name": c.name,
            "datatype": c.vot_type[0],
            "unit": c.unit,
            "ucd": c.ucd,
            "description": c.description,
        }
        for c in columns
    ]
    yield (f'{{"metadata": {json.dumps(metadata)}, "data": [').encode()
    first = True
    buffer: list = []

    def dump(batch: list) -> bytes:
        # One encoder call for the whole batch rather than one per row. A
        # list dumps as `[a, b], [c, d]` between its outer brackets, with the
        # separator json already uses, so trimming them yields exactly the
        # rows this used to join by hand.
        text = json.dumps(batch)[1:-1]
        return text.encode() if first else f", {text}".encode()

    for row in _coerced_rows(rows, _coercion_plan(columns, _VALUE_COERCIONS)):
        buffer.append(row)
        if len(buffer) >= 500:
            yield dump(buffer)
            first = False
            buffer.clear()
    if buffer:
        yield dump(buffer)
    yield (f'], "status": {json.dumps(rows.status)}}}').encode()


# ---------------------------------------------------------------------------
# Arrow / Parquet
# ---------------------------------------------------------------------------

_ARROW_BATCH = 10_000


class _DrainableSink(io.RawIOBase):
    """Append-only sink whose written bytes are drained incrementally, so
    sequential writers (Parquet, Arrow IPC) can be streamed chunk by chunk."""

    def __init__(self):
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def _arrow_schema(columns: list[ColumnMeta]):
    import pyarrow as pa

    types = {
        "bool": pa.bool_(),
        "int16": pa.int16(),
        "int32": pa.int32(),
        "int64": pa.int64(),
        "float32": pa.float32(),
        "float64": pa.float64(),
        "decimal": pa.float64(),  # as VOTable declares it
        "str": pa.string(),
        "opaque": pa.string(),
        "timestamp": pa.string(),  # ISO-8601, consistent with the text formats
    }
    fields = []
    for col in columns:
        metadata = {
            key: value
            for key, value in (
                ("unit", col.unit),
                ("ucd", col.ucd),
                ("description", col.description),
            )
            if value
        }
        fields.append(pa.field(col.name, types[col.kind], metadata=metadata or None))
    return pa.schema(fields)


def _arrow_batches(columns: list[ColumnMeta], rows: RowLimiter, schema):
    import pyarrow as pa

    coercions = [_ARROW_COERCIONS.get(col.kind) for col in columns]

    def columnar(batch: list[tuple]):
        # `zip(*batch)` transposes in C. The comprehension it replaced indexed
        # every cell from Python and called a converter on each of them, which
        # on a wide result is the transpose done the slow way twice over.
        return [
            list(values) if coerce is None else [None if v is None else coerce(v) for v in values]
            for values, coerce in zip(zip(*batch, strict=True), coercions, strict=True)
        ]

    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= _ARROW_BATCH:
            yield pa.record_batch(columnar(batch), schema=schema)
            batch.clear()
    if batch:
        yield pa.record_batch(columnar(batch), schema=schema)


def stream_parquet(columns: list[ColumnMeta], rows: RowLimiter) -> Iterator[bytes]:
    import pyarrow.parquet as pq

    schema = _arrow_schema(columns)
    sink = _DrainableSink()
    writer = pq.ParquetWriter(sink, schema, compression="zstd")
    wrote_any = False
    for batch in _arrow_batches(columns, rows, schema):
        writer.write_batch(batch)
        wrote_any = True
        yield sink.drain()
    if not wrote_any:  # emit an empty (schema-only) file
        import pyarrow as pa

        writer.write_table(pa.Table.from_batches([], schema=schema))
    writer.add_key_value_metadata({"IVOA.VOTable.QUERY_STATUS": rows.status})
    writer.close()
    yield sink.drain()


def stream_arrow(columns: list[ColumnMeta], rows: RowLimiter) -> Iterator[bytes]:
    import pyarrow as pa

    schema = _arrow_schema(columns)
    sink = _DrainableSink()
    with pa.ipc.new_stream(sink, schema) as writer:
        for batch in _arrow_batches(columns, rows, schema):
            writer.write_batch(batch)
            yield sink.drain()
    yield sink.drain()


SERIALIZERS = {
    "votable": stream_votable,
    "csv": lambda c, r: stream_dsv(c, r, ","),
    "tsv": lambda c, r: stream_dsv(c, r, "\t"),
    "json": stream_json,
    "parquet": stream_parquet,
    "arrow": stream_arrow,
}


def stream(columns: list[ColumnMeta], rows: RowLimiter, fmt_key: str) -> Iterator[bytes]:
    try:
        serializer = SERIALIZERS[fmt_key]
    except KeyError:
        raise ValueError(f"unknown format key {fmt_key}") from None
    return serializer(columns, rows)
