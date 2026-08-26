"""Shared query preparation and synchronous (streaming) execution."""

import contextlib
import functools
import itertools
import threading
import time
from collections.abc import Iterator

from egernia_core.config import settings
from egernia_core.db import StreamedRows
from egernia_core.db import connection as db_connection
from egernia_core.errors import UsageError
from egernia_core.metadata.schema_gen import REGION_SUFFIX
from egernia_core.observability import QUERY_DURATION, tag_sql
from egernia_core.query.adql import apply_maxrec, check_language, translate
from egernia_core.query.results import RowLimiter, columns_from_cursor, stream, tap_schema_metadata
from egernia_core.query.upload import (
    UploadedTable,
    create_upload_tables,
    parse_upload_param,
    rewrite_upload_refs,
)
from egernia_core.query.votable import normalize_format

from .params import require

STREAM_CHUNK_ROWS = 2000


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
    unpublished = _first_unpublished(tables, _published_tables(), upload_names)
    if unpublished is not None:
        # The table list is cached, so "not published" may only mean "not
        # published when we last looked". A table registered a moment ago must
        # not be refused for up to the cache's lifetime, so refusing is what
        # forces a fresh read — rare, and cheap because it is rare.
        unpublished = _first_unpublished(tables, _published_tables(refresh=True), upload_names)
    if unpublished is not None:
        raise UsageError(f"table {unpublished} is not published by this service")

    # Geometry slots accept any bare identifier during translation, which is
    # pure and consults no schema. A text column reaching PostgreSQL there
    # fails as `operator does not exist: text && scircle` — a server-fault
    # shape for a usage error, naming neither the column at fault nor the one
    # to use. Refused here instead, where the column metadata already is.
    _reject_text_geometry(translation.geometry_columns, tables, upload_names)

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


def _first_unpublished(
    tables: frozenset[str], published: frozenset[str], upload_names: set[str]
) -> str | None:
    """The first table that is not readable, or None if all of them are.

    Uploads are checked here too, but against the request's own uploads, so
    they are never affected by the cached list.
    """
    for table in tables:
        lower = table.lower()
        if lower.startswith("tap_upload."):
            if lower.removeprefix("tap_upload.") not in upload_names:
                raise UsageError(f"table {table} was not uploaded with this request")
        elif lower not in published:
            return table
    return None


# TAP_SCHEMA's table list changes when a deployment gains a metadata domain or
# an operator publishes a table — rarely, and never per request, which is how
# often this used to be read. Cached with a short life so a table published
# out of band still appears without a restart, and invalidated outright when
# this service is the one that changed it.
_PUBLISHED_TTL_S = 30.0
_published_cache: tuple[float, frozenset[str]] | None = None
_published_lock = threading.Lock()


def _published_tables(*, refresh: bool = False) -> frozenset[str]:
    """The published table names, from the cache unless it is stale.

    ``refresh`` re-reads under the lock rather than clearing and re-reading,
    so a concurrent reader cannot slot a stale result in between the two —
    which is what a caller asking for fresh data is trying to avoid.
    """
    global _published_cache
    asked_at = time.monotonic()
    cached = _published_cache
    if not refresh and cached is not None and asked_at - cached[0] < _PUBLISHED_TTL_S:
        return cached[1]
    with _published_lock:
        cached = _published_cache
        if cached is not None:
            # A refresh wants a read newer than this call — including one
            # another thread completed while this one waited on the lock,
            # which is what stops concurrent misses becoming a stampede of
            # identical queries.
            fresh_enough = (
                cached[0] >= asked_at
                if refresh
                else time.monotonic() - cached[0] < _PUBLISHED_TTL_S
            )
            if fresh_enough:
                return cached[1]
        with db_connection() as conn:
            rows = conn.execute("SELECT table_name FROM tap_schema.tables").fetchall()
        tables = frozenset(r[0].lower() for r in rows)
        _published_cache = (time.monotonic(), tables)
        return tables


# pgsphere's spatial types. A column is queryable with INTERSECTS/CONTAINS
# exactly when PostgreSQL holds it as one of these; TAP_SCHEMA cannot answer
# this, because its VOTable datatype for spoly is "char" — the same as the
# STC-S text the footprint was derived from.
_GEOMETRY_TYPES = frozenset({"spoly", "spoint", "scircle", "sbox", "spath", "sline"})


@functools.cache
def _column_is_geometry() -> dict[str, bool]:
    """``schema.table.column`` -> whether PostgreSQL holds it as a geometry.

    Read once and kept, rather than given the TTL and refresh-under-lock
    dance ``_published_tables`` needs, because the two go stale differently.
    A stale table list refuses a table that *is* published, which a user
    sees immediately; this map only decides whether a geometry predicate is
    refused, and a column it has never heard of is left alone — so a stale
    read costs the behaviour that existed before this check rather than a
    wrong refusal. It is dropped with the table list whenever this service
    changes the schema, which is the only way the answer moves in practice.
    """
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT table_schema, table_name, column_name, udt_name"
            " FROM information_schema.columns"
            " WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        ).fetchall()
    return {
        f"{schema}.{table}.{column}".lower(): udt in _GEOMETRY_TYPES
        for schema, table, column, udt in rows
    }


def _reject_text_geometry(
    references: frozenset[str], tables: frozenset[str], upload_names: set[str]
) -> None:
    """Refuse a geometry predicate over a column that is not a geometry.

    Only a column positively identified as non-geometry in a table the query
    reads is refused. A reference that cannot be resolved — an alias, an
    uploaded table, a column added since the last catalogue read — is left
    alone: translating it was already legal, and guessing would refuse valid
    queries to catch a mistake PostgreSQL still reports.
    """
    if not references:
        return
    columns = _column_is_geometry()
    readable = [t.lower() for t in tables if not t.lower().startswith("tap_upload.")]
    if upload_names or not readable:
        return
    for reference in sorted(references):
        bare = reference.rsplit(".", 1)[-1].lower()
        found = [columns[key] for t in readable if (key := f"{t}.{bare}") in columns]
        if not found or any(found):
            continue
        companion = f"{bare}{REGION_SUFFIX}"
        alternative = next(
            (companion for t in readable if columns.get(f"{t}.{companion}")),
            None,
        )
        message = (
            f"column {reference} holds STC-S text, so it cannot be used in a"
            " geometry predicate such as INTERSECTS or CONTAINS"
        )
        if alternative:
            # Named rather than substituted: the companion is nullable, so a
            # row whose STC-S failed to convert at ingestion is present under
            # one column and absent under the other. Answering a different
            # query than the one asked would drop those rows silently.
            message += (
                f". Use {alternative}, the pgsphere footprint derived from it at"
                f" ingestion — note it is NULL wherever {bare} could not be converted"
            )
        raise UsageError(message)


def forget_published_tables() -> None:
    """Drop what this service caches about the schema, for when it changes it.

    Both caches, because publishing a table can also add the geometry column
    a predicate over it would be judged against.
    """
    global _published_cache
    with _published_lock:
        _published_cache = None
    _column_is_geometry.cache_clear()


def _result_chunks(prepared: dict, uploads: list[UploadedTable]) -> Iterator[bytes]:
    """Execute the prepared query as a streamed statement and yield the
    serialized result chunk by chunk; the connection stays checked out
    until the stream is exhausted."""
    sql = apply_maxrec(prepared["sql"], prepared["maxrec"])
    if uploads:
        sql = rewrite_upload_refs(sql, {u.name for u in uploads})
    # tagged with the request id: a statement in pg_stat_activity, or in the
    # server log, then names the request it came from
    sql = tag_sql(sql)
    with db_connection() as conn, conn.transaction():
        tap_meta = tap_schema_metadata(conn, prepared["tables"])
        if uploads:
            create_upload_tables(conn, uploads, settings.query_role)
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(settings.sync_timeout_s * 1000),),
        )
        conn.execute("SET LOCAL jit = off")  # uninterruptible compile stalls
        conn.execute(f"SET LOCAL ROLE {settings.query_role}")
        # A plain streamed statement, not a DECLARE'd cursor: the planner may
        # use parallel workers and plans for the whole result, which a cursor
        # forbids — on a full-table aggregate that is most of the query's
        # cost. statement_timeout consequently bounds the whole statement,
        # streaming included, which is what a sync timeout should mean.
        with conn.cursor() as cur:
            rows = StreamedRows(cur, sql, chunk_rows=STREAM_CHUNK_ROWS)
            with contextlib.closing(rows):
                columns = columns_from_cursor(cur.description, tap_meta)
                limiter = RowLimiter(rows, prepared["maxrec"])
                yield from stream(columns, limiter, prepared["fmt_key"])


def run_sync(
    prepared: dict, uploads: list[UploadedTable] | None = None
) -> tuple[Iterator[bytes], str]:
    """Start the query and return (chunk iterator, mime type).

    The first chunk is produced eagerly so translation/permission errors
    still surface as proper DALI/JSON error responses instead of dying
    mid-stream.
    """
    started = time.perf_counter()
    chunks = _result_chunks(prepared, uploads or [])
    first = next(chunks, b"")
    return _timed(itertools.chain([first], chunks), started), prepared["mime"]


def _timed(chunks: Iterator[bytes], started: float) -> Iterator[bytes]:
    """Record the query's duration when its stream ends.

    Observed at the end, not after the first chunk: the metric says "to the
    last row", and stopping at the first would have made it time-to-first-byte
    and under-reported exactly the long streams worth knowing about. Recorded
    in a finally, so a client that disconnects halfway is still measured
    rather than silently dropped.
    """
    try:
        yield from chunks
    finally:
        QUERY_DURATION.labels(kind="sync").observe(time.perf_counter() - started)
