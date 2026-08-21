"""Result-set serialization: VOTable (via astropy), CSV, TSV, JSON.

TAP/DALI require VOTable as the default output format, with an INFO element
named QUERY_STATUS carrying OK / ERROR / OVERFLOW.
"""

import csv
import datetime
import io
import json
from decimal import Decimal
from xml.sax.saxutils import escape

import numpy as np
from astropy.io.votable import from_table
from astropy.io.votable.tree import Info
from astropy.table import MaskedColumn, Table

# RESPONSEFORMAT aliases -> (canonical key, mime type, file extension)
FORMATS = {
    "votable": ("votable", "application/x-votable+xml", "vot"),
    "application/x-votable+xml": ("votable", "application/x-votable+xml", "vot"),
    "text/xml": ("votable", "application/x-votable+xml", "vot"),
    "csv": ("csv", "text/csv", "csv"),
    "text/csv": ("csv", "text/csv", "csv"),
    "tsv": ("tsv", "text/tab-separated-values", "tsv"),
    "text/tab-separated-values": ("tsv", "text/tab-separated-values", "tsv"),
    "json": ("json", "application/json", "json"),
    "application/json": ("json", "application/json", "json"),
}

DEFAULT_FORMAT = "votable"


def normalize_format(fmt: str | None) -> tuple[str, str, str]:
    if not fmt:
        fmt = DEFAULT_FORMAT
    try:
        return FORMATS[fmt.strip().lower()]
    except KeyError:
        raise ValueError(f"RESPONSEFORMAT={fmt} is not supported") from None


def _convert(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _masked_column(name: str, values: list) -> MaskedColumn:
    converted = [_convert(v) for v in values]
    mask = [v is None for v in converted]
    present = [v for v in converted if v is not None]
    if any(isinstance(v, str) for v in present):
        converted = ["" if v is None else str(v) for v in converted]
    elif any(isinstance(v, float) for v in present):
        converted = [np.nan if v is None else float(v) for v in converted]
    elif present and all(isinstance(v, bool) for v in present):
        converted = [False if v is None else v for v in converted]
    elif any(isinstance(v, int) for v in present):
        converted = [0 if v is None else int(v) for v in converted]
    else:  # all null
        converted = ["" for _ in converted]
    return MaskedColumn(converted, name=name, mask=mask)


def serialize(
    names: list[str], rows: list[tuple], fmt_key: str, status: str = "OK"
) -> bytes:
    """Serialize a result set in the given canonical format."""
    if fmt_key == "votable":
        return _to_votable(names, rows, status)
    if fmt_key in ("csv", "tsv"):
        out = io.StringIO()
        writer = csv.writer(out, delimiter="," if fmt_key == "csv" else "\t")
        writer.writerow(names)
        for row in rows:
            writer.writerow(["" if v is None else _convert(v) for v in row])
        return out.getvalue().encode("utf-8")
    if fmt_key == "json":
        payload = {
            "status": status,
            "metadata": [{"name": n} for n in names],
            "data": [[_convert(v) for v in row] for row in rows],
        }
        return json.dumps(payload).encode("utf-8")
    raise ValueError(f"unknown format key {fmt_key}")


def _to_votable(names: list[str], rows: list[tuple], status: str) -> bytes:
    if rows:
        table = Table([_masked_column(n, [r[i] for r in rows]) for i, n in enumerate(names)])
    else:
        table = Table([MaskedColumn([], name=n, dtype="U1") for n in names])
    vot = from_table(table)
    vot.resources[0].type = "results"
    vot.resources[0].infos.append(Info(name="QUERY_STATUS", value=status))
    buf = io.BytesIO()
    vot.to_xml(buf)
    return buf.getvalue()


def error_votable(message: str) -> bytes:
    """DALI error document: VOTable with QUERY_STATUS=ERROR."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">\n'
        '  <RESOURCE type="results">\n'
        f'    <INFO name="QUERY_STATUS" value="ERROR">{escape(message)}</INFO>\n'
        "  </RESOURCE>\n"
        "</VOTABLE>\n"
    ).encode("utf-8")
