"""Typed, streaming result serialization.

Columns are typed from the PostgreSQL cursor description (type OIDs) and
enriched with unit/UCD/description drawn from TAP_SCHEMA.columns for the
tables the query touches. Serializers are generators of byte chunks fed
from a server-side cursor, so result sets are never fully materialized:
VOTable (TABLEDATA), CSV, TSV, JSON, Parquet, and an Arrow IPC stream.

DALI overflow is detected by fetching one row past MAXREC; formats that
carry a status report OK/OVERFLOW (the VOTable trailing INFO, the JSON
status field, Parquet/Arrow custom metadata).
"""

import csv
import datetime
import io
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from xml.sax.saxutils import escape, quoteattr

# canonical kind -> (VOTable datatype, VOTable arraysize, VOTable xtype)
_VOT_TYPES = {
    "bool": ("boolean", None, None),
    "int16": ("short", None, None),
    "int32": ("int", None, None),
    "int64": ("long", None, None),
    "float32": ("float", None, None),
    "float64": ("double", None, None),
    "str": ("char", "*", None),
    "timestamp": ("char", "*", "timestamp"),
}

# PostgreSQL type OID -> canonical kind
_OID_KINDS = {
    16: "bool",  # bool
    21: "int16",  # int2
    23: "int32",  # int4
    20: "int64",  # int8
    700: "float32",  # float4
    701: "float64",  # float8
    1700: "float64",  # numeric
    1082: "timestamp",  # date
    1083: "timestamp",  # time
    1114: "timestamp",  # timestamp
    1184: "timestamp",  # timestamptz
}


@dataclass
class ColumnMeta:
    name: str
    kind: str = "str"
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
    for name, unit, ucd, description in rows:
        entry = {"unit": unit, "ucd": ucd, "description": description}
        if name in meta and (meta[name]["unit"], meta[name]["ucd"]) != (unit, ucd):
            meta[name] = {"unit": None, "ucd": None, "description": None}
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
                kind=_OID_KINDS.get(entry.type_code, "str"),
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
    """Convert a DB value to a JSON/CSV-friendly primitive."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _vot_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    value = _plain(value)
    return escape(str(value))


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
                f"<DESCRIPTION>{escape(col.description)}</DESCRIPTION></FIELD>\n"
            )
        else:
            head.append(f"<FIELD {' '.join(attrs)}/>\n")
    head.append("<DATA><TABLEDATA>\n")
    yield "".join(head).encode()

    buffer: list[str] = []
    for row in rows:
        cells = "".join(
            "<TD/>" if value is None else f"<TD>{_vot_text(value)}</TD>" for value in row
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
    for row in rows:
        writer.writerow(["" if v is None else _plain(v) for v in row])
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
    buffer: list[str] = []
    for row in rows:
        text = json.dumps([_plain(v) for v in row])
        buffer.append(text if first else f", {text}")
        first = False
        if len(buffer) >= 500:
            yield "".join(buffer).encode()
            buffer.clear()
    if buffer:
        yield "".join(buffer).encode()
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
        "str": pa.string(),
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

    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= _ARROW_BATCH:
            yield pa.record_batch(
                [[_plain(r[i]) for r in batch] for i in range(len(columns))], schema=schema
            )
            batch.clear()
    if batch:
        yield pa.record_batch(
            [[_plain(r[i]) for r in batch] for i in range(len(columns))], schema=schema
        )


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
