"""RESPONSEFORMAT handling and DALI error documents.

Result serialization itself lives in tapcore.query.results (typed, streaming).
The convenience serialize() here materializes a stream for small payloads.
"""

from xml.sax.saxutils import escape

from .results import ColumnMeta, RowLimiter, stream

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
    "parquet": ("parquet", "application/vnd.apache.parquet", "parquet"),
    "application/vnd.apache.parquet": ("parquet", "application/vnd.apache.parquet", "parquet"),
    "arrow": ("arrow", "application/vnd.apache.arrow.stream", "arrows"),
    "application/vnd.apache.arrow.stream": (
        "arrow",
        "application/vnd.apache.arrow.stream",
        "arrows",
    ),
}

DEFAULT_FORMAT = "votable"


def normalize_format(fmt: str | None) -> tuple[str, str, str]:
    if not fmt:
        fmt = DEFAULT_FORMAT
    try:
        return FORMATS[fmt.strip().lower()]
    except KeyError:
        raise ValueError(f"RESPONSEFORMAT={fmt} is not supported") from None


def serialize(
    columns: list[ColumnMeta] | list[str],
    rows: list[tuple],
    fmt_key: str,
    maxrec: int | None = None,
) -> bytes:
    """Materialize a (small) result set in the given canonical format.

    Accepts plain column names for convenience; MAXREC defaults to the row
    count, i.e. no overflow.
    """
    metas = [ColumnMeta(name=c) if isinstance(c, str) else c for c in columns]
    limiter = RowLimiter(rows, len(rows) if maxrec is None else maxrec)
    return b"".join(stream(metas, limiter, fmt_key))


def error_votable(message: str) -> bytes:
    """DALI error document: VOTable with QUERY_STATUS=ERROR."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">\n'
        '  <RESOURCE type="results">\n'
        f'    <INFO name="QUERY_STATUS" value="ERROR">{escape(message)}</INFO>\n'
        "  </RESOURCE>\n"
        "</VOTABLE>\n"
    ).encode()
