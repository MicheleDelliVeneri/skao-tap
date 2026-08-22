"""Shared query preparation and synchronous (streaming) execution."""

import itertools
from collections.abc import Iterator

from tapcore.adql import adql_to_postgresql, apply_maxrec, check_language, touched_tables
from tapcore.config import settings
from tapcore.db import pool
from tapcore.errors import UsageError
from tapcore.results import RowLimiter, columns_from_cursor, stream, tap_schema_metadata
from tapcore.upload import (
    UploadedTable,
    create_upload_tables,
    parse_upload_param,
    rewrite_upload_refs,
)
from tapcore.votable import normalize_format

from .params import require

CURSOR_ITERSIZE = 2000


def prepare_query(params: dict[str, str]) -> dict:
    """Validate TAP parameters and translate the ADQL query.

    Returns a dict with the translated sql, touched tables, maxrec, and
    output format info.
    """
    request = params.get("REQUEST")  # required in TAP 1.0, deprecated in 1.1
    if request and request not in ("doQuery",):
        raise UsageError(f"REQUEST={request} is not supported")
    upload_names = (
        {name.lower() for name, _ in parse_upload_param(params["UPLOAD"])}
        if params.get("UPLOAD")
        else set()
    )

    check_language(params.get("LANG", "ADQL"))
    query = require(params, "QUERY")
    sql = adql_to_postgresql(query)

    tables = touched_tables(sql)
    published = _published_tables()
    for table in tables:
        lower = table.lower()
        if lower.startswith("tap_upload."):
            if lower.removeprefix("tap_upload.") not in upload_names:
                raise UsageError(f"table {table} was not uploaded with this request")
        elif lower not in published:
            raise UsageError(f"table {table} is not published by this service")

    maxrec = params.get("MAXREC")
    if maxrec is not None:
        try:
            maxrec = int(maxrec)
        except ValueError:
            raise UsageError(f"MAXREC={maxrec} is not an integer") from None
        if maxrec < 0:
            raise UsageError("MAXREC must be >= 0")
        maxrec = min(maxrec, settings.hard_maxrec)
    else:
        maxrec = settings.default_maxrec

    fmt = params.get("RESPONSEFORMAT") or params.get("FORMAT")
    try:
        fmt_key, mime, ext = normalize_format(fmt)
    except ValueError as exc:
        raise UsageError(str(exc)) from None

    return {
        "sql": sql,
        "tables": tables,
        "upload_names": upload_names,
        "maxrec": maxrec,
        "fmt_key": fmt_key,
        "mime": mime,
        "ext": ext,
    }


def _published_tables() -> set[str]:
    with pool().connection() as conn:
        rows = conn.execute("SELECT table_name FROM tap_schema.tables").fetchall()
    return {r[0].lower() for r in rows}


def _result_chunks(prepared: dict, uploads: list[UploadedTable]) -> Iterator[bytes]:
    """Execute the prepared query on a server-side cursor and yield the
    serialized result chunk by chunk; the connection stays checked out
    until the stream is exhausted."""
    sql = apply_maxrec(prepared["sql"], prepared["maxrec"])
    if uploads:
        sql = rewrite_upload_refs(sql, {u.name for u in uploads})
    with pool().connection() as conn, conn.transaction():
        tap_meta = tap_schema_metadata(conn, prepared["tables"])
        if uploads:
            create_upload_tables(conn, uploads, settings.query_role)
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(settings.sync_timeout_s * 1000),),
        )
        conn.execute("SET LOCAL jit = off")  # uninterruptible compile stalls
        conn.execute(f"SET LOCAL ROLE {settings.query_role}")
        with conn.cursor(name="tap_sync") as cur:
            cur.itersize = CURSOR_ITERSIZE
            cur.execute(sql)
            columns = columns_from_cursor(cur.description, tap_meta)
            limiter = RowLimiter(cur, prepared["maxrec"])
            yield from stream(columns, limiter, prepared["fmt_key"])


def run_sync(
    prepared: dict, uploads: list[UploadedTable] | None = None
) -> tuple[Iterator[bytes], str]:
    """Start the query and return (chunk iterator, mime type).

    The first chunk is produced eagerly so translation/permission errors
    still surface as proper DALI/JSON error responses instead of dying
    mid-stream.
    """
    chunks = _result_chunks(prepared, uploads or [])
    first = next(chunks, b"")
    return itertools.chain([first], chunks), prepared["mime"]
