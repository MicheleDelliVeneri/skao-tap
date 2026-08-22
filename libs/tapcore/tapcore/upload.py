"""TAP table upload (``UPLOAD``): VOTable parsing and per-query temp tables.

An uploaded table is materialized as a PostgreSQL temporary table
(``pg_temp.tap_upload_<name>``, dropped at commit) inside the transaction
that runs the query, and the translated SQL's ``TAP_UPLOAD.<name>``
references are rewritten to it. Shared by the sync path (tap-api) and the
async executor, which re-creates the tables from the VOTables persisted
with the job.

Only the TABLEDATA VOTable serialization is accepted; BINARY/BINARY2 and
FITS uploads are rejected with a UsageError.
"""

import os
import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .errors import UsageError
from .uws import job_results_dir

UPLOAD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# VOTable datatype -> PostgreSQL column type (scalars)
_PG_TYPES = {
    "boolean": "boolean",
    "unsignedByte": "smallint",
    "short": "smallint",
    "int": "integer",
    "long": "bigint",
    "float": "real",
    "double": "double precision",
    "char": "text",
    "unicodeChar": "text",
}

_TRUE = {"true", "t", "1"}
_FALSE = {"false", "f", "0"}


@dataclass
class UploadedTable:
    name: str  # ADQL name: TAP_UPLOAD.<name>
    columns: list[tuple[str, str]]  # (column name, PostgreSQL type)
    rows: list[tuple]

    @property
    def ident(self) -> str:
        return table_ident(self.name)


def table_ident(name: str) -> str:
    """The temp-table identifier backing TAP_UPLOAD.<name>."""
    return f"pg_temp.tap_upload_{name.lower()}"


def parse_upload_param(value: str) -> list[tuple[str, str]]:
    """Parse a DALI UPLOAD parameter: ``name,uri`` pairs separated by ``;``.

    Returns [(table name, uri)]; the uri is ``param:<part>`` for inline
    multipart uploads or an http(s) URL.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in value.split(";"):
        item = item.strip()
        if not item:
            continue
        name, sep, uri = item.partition(",")
        name, uri = name.strip(), uri.strip()
        if not sep or not uri:
            raise UsageError(f"UPLOAD entry {item!r} is not of the form table,uri")
        if not UPLOAD_NAME_RE.fullmatch(name):
            raise UsageError(f"invalid UPLOAD table name {name!r}")
        key = name.lower()
        if key in seen:
            raise UsageError(f"duplicate UPLOAD table name {name}")
        seen.add(key)
        pairs.append((name, uri))
    if not pairs:
        raise UsageError("UPLOAD parameter contains no table,uri pairs")
    return pairs


def _local(tag: str) -> str:
    """Element tag without the VOTable namespace (any version)."""
    return tag.rsplit("}", 1)[-1]


def _pg_type(field: ET.Element) -> str:
    datatype = field.get("datatype", "char")
    arraysize = field.get("arraysize")
    if field.get("xtype") == "timestamp":
        return "timestamp"
    if datatype in ("char", "unicodeChar"):
        return "text"
    if arraysize not in (None, "1"):
        # non-character arrays are stored as text (their TD literal)
        return "text"
    try:
        return _PG_TYPES[datatype]
    except KeyError:
        raise UsageError(f"unsupported VOTable datatype {datatype!r} in upload") from None


def _convert(text: str | None, pg_type: str):
    if text is None:
        return None
    if pg_type in ("smallint", "integer", "bigint"):
        stripped = text.strip()
        return int(stripped) if stripped else None
    if pg_type in ("real", "double precision"):
        stripped = text.strip()
        if not stripped or stripped.lower() == "nan":
            return None
        return float(stripped)
    if pg_type == "boolean":
        stripped = text.strip().lower()
        if stripped in _TRUE:
            return True
        if stripped in _FALSE:
            return False
        return None
    return text


def parse_votable(name: str, data: bytes, max_rows: int, max_bytes: int) -> UploadedTable:
    """Parse an uploaded VOTable (TABLEDATA serialization) into an
    UploadedTable, enforcing the configured size limits."""
    if len(data) > max_bytes:
        raise UsageError(f"upload {name} exceeds the {max_bytes} byte limit")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise UsageError(f"upload {name} is not well-formed XML: {exc}") from None
    if _local(root.tag) != "VOTABLE":
        raise UsageError(f"upload {name} is not a VOTable document")

    table = next((el for el in root.iter() if _local(el.tag) == "TABLE"), None)
    if table is None:
        raise UsageError(f"upload {name} contains no TABLE")
    for el in table.iter():
        if _local(el.tag) in ("BINARY", "BINARY2", "FITS"):
            raise UsageError(
                f"upload {name}: only the TABLEDATA VOTable serialization is supported"
            )

    fields = [el for el in table if _local(el.tag) == "FIELD"]
    if not fields:
        raise UsageError(f"upload {name} declares no FIELDs")
    columns: list[tuple[str, str]] = []
    seen: set[str] = set()
    for field in fields:
        col = (field.get("name") or "").strip().lower()
        if not UPLOAD_NAME_RE.fullmatch(col):
            raise UsageError(f"upload {name}: invalid column name {field.get('name')!r}")
        if col in seen:
            raise UsageError(f"upload {name}: duplicate column name {col}")
        seen.add(col)
        columns.append((col, _pg_type(field)))

    tabledata = next((el for el in table.iter() if _local(el.tag) == "TABLEDATA"), None)
    rows: list[tuple] = []
    for tr in tabledata if tabledata is not None else []:
        if _local(tr.tag) != "TR":
            continue
        cells = [td for td in tr if _local(td.tag) == "TD"]
        if len(cells) != len(columns):
            raise UsageError(
                f"upload {name}: row {len(rows) + 1} has {len(cells)} cells,"
                f" expected {len(columns)}"
            )
        if len(rows) >= max_rows:
            raise UsageError(f"upload {name} exceeds the {max_rows} row limit")
        rows.append(tuple(_convert(td.text, pg[1]) for td, pg in zip(cells, columns, strict=True)))
    return UploadedTable(name=name, columns=columns, rows=rows)


_INSERT_BATCH = 500


def create_upload_tables(conn, uploads: list[UploadedTable], query_role: str) -> None:
    """Create and fill the temp tables inside the current transaction and
    grant SELECT to the (read-only) query role that runs the user query."""
    for upload in uploads:
        columns = ", ".join(f"{col} {pg_type}" for col, pg_type in upload.columns)
        conn.execute(
            f"CREATE TEMP TABLE {upload.ident.split('.', 1)[1]} ({columns}) ON COMMIT DROP"
        )
        conn.execute(f"GRANT SELECT ON {upload.ident} TO {query_role}")
        col_names = ", ".join(col for col, _ in upload.columns)
        row_tpl = "(" + ", ".join(["%s"] * len(upload.columns)) + ")"
        for start in range(0, len(upload.rows), _INSERT_BATCH):
            batch = upload.rows[start : start + _INSERT_BATCH]
            conn.execute(
                f"INSERT INTO {upload.ident} ({col_names}) VALUES "
                + ", ".join([row_tpl] * len(batch)),
                tuple(value for row in batch for value in row),
            )


def uploads_dir(job_id: str) -> str:
    """Where an async job's uploaded VOTables are persisted (under the
    job's results directory, so job deletion cleans them up too)."""
    return os.path.join(job_results_dir(job_id), "uploads")


def save_upload_sources(job_id: str, sources: dict[str, bytes]) -> None:
    directory = uploads_dir(job_id)
    os.makedirs(directory, exist_ok=True)
    for name, data in sources.items():
        with open(os.path.join(directory, f"{name.lower()}.vot"), "wb") as fh:
            fh.write(data)


def load_uploads(
    job_id: str, names: list[str], max_rows: int, max_bytes: int
) -> list[UploadedTable]:
    """Re-parse the persisted VOTables when the executor runs the job."""
    directory = uploads_dir(job_id)
    uploads = []
    for name in names:
        path = os.path.join(directory, f"{name.lower()}.vot")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            raise UsageError(f"uploaded table {name} is missing for job {job_id}") from None
        uploads.append(parse_votable(name, data, max_rows, max_bytes))
    return uploads


def rewrite_upload_refs(sql: str, names: set[str]) -> str:
    """Rewrite ``TAP_UPLOAD.<name>`` references in the translated SQL to the
    backing temp tables."""
    for name in names:
        sql = re.sub(
            rf"\bTAP_UPLOAD\.{re.escape(name)}\b",
            table_ident(name),
            sql,
            flags=re.IGNORECASE,
        )
    return sql
