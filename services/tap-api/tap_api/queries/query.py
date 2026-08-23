"""Shared query preparation and synchronous (streaming) execution."""

import itertools
import threading
import time
from collections.abc import Iterator

from tapcore.config import settings
from tapcore.db import pool
from tapcore.errors import UsageError
from tapcore.query.adql import apply_maxrec, check_language, translate
from tapcore.query.results import RowLimiter, columns_from_cursor, stream, tap_schema_metadata
from tapcore.query.upload import (
    UploadedTable,
    create_upload_tables,
    parse_upload_param,
    rewrite_upload_refs,
)
from tapcore.query.votable import normalize_format

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
    translation = translate(query)
    sql = translation.sql

    tables = translation.tables
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


# TAP_SCHEMA's table list changes when a deployment gains a metadata domain or
# an operator publishes a table — rarely, and never per request, which is how
# often this used to be read. Cached with a short life so a table published
# out of band still appears without a restart, and invalidated outright when
# this service is the one that changed it.
_PUBLISHED_TTL_S = 30.0
_published_cache: tuple[float, frozenset[str]] | None = None
_published_lock = threading.Lock()


def _published_tables() -> frozenset[str]:
    global _published_cache
    now = time.monotonic()
    cached = _published_cache
    if cached is not None and now - cached[0] < _PUBLISHED_TTL_S:
        return cached[1]
    with _published_lock:
        # another thread may have refreshed it while this one waited
        cached = _published_cache
        if cached is not None and time.monotonic() - cached[0] < _PUBLISHED_TTL_S:
            return cached[1]
        with pool().connection() as conn:
            rows = conn.execute("SELECT table_name FROM tap_schema.tables").fetchall()
        tables = frozenset(r[0].lower() for r in rows)
        _published_cache = (time.monotonic(), tables)
        return tables


def forget_published_tables() -> None:
    """Drop the cached table list, for when this service publishes a table."""
    global _published_cache
    _published_cache = None


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
