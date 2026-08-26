"""RESPONSEFORMAT handling and DALI error documents.

Result serialization itself lives in egernia_core.query.results (typed, streaming).
"""

from xml.sax.saxutils import escape

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
