"""RESPONSEFORMAT handling and DALI error documents.

Result serialization itself lives in egernia_core.query.results (typed, streaming).
"""

from xml.sax.saxutils import escape

#: canonical key -> (mime type, file extension)
_FORMATS = {
    "votable": ("application/x-votable+xml", "vot"),
    "csv": ("text/csv", "csv"),
    "tsv": ("text/tab-separated-values", "tsv"),
    "json": ("application/json", "json"),
    "parquet": ("application/vnd.apache.parquet", "parquet"),
    "arrow": ("application/vnd.apache.arrow.stream", "arrows"),
}

# RESPONSEFORMAT aliases -> (canonical key, mime type, file extension): every
# format answers to its short name and to its own mime type. text/xml is the
# one alias that is neither, kept because TAP 1.0 clients send it.
FORMATS = {
    alias: (key, mime, ext) for key, (mime, ext) in _FORMATS.items() for alias in (key, mime)
}
FORMATS["text/xml"] = FORMATS["votable"]

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
