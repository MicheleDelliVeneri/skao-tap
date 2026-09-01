"""PostgreSQL access via a shared psycopg3 connection pool."""

import contextlib

import psycopg
from psycopg_pool import ConnectionPool

from .config import settings
from .observability import DB_CONNECTIONS_IN_USE, pool_wait_timer

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            # bound the wait, so exhaustion is a quick answer rather than a
            # request that hangs for psycopg's 30s default and then 500s
            timeout=settings.db_pool_timeout_s,
            open=True,
        )
    return _pool


@contextlib.contextmanager
def connection():
    """A pooled connection, with the wait for it measured.

    Prefer this to ``pool().connection()``: waiting for a connection is the
    service's real backpressure signal, and it must stay visible.

    Only the acquisition is timed, never the held time. Timing the whole
    block — which is what a combined ``with`` does, since the caller's work
    happens at the yield — would add query and streaming time to the wait
    (a sync query holds its connection for the length of the client's
    download), turning the one metric that reports backpressure into a
    slow-response metric.
    """
    with contextlib.ExitStack() as stack:
        with pool_wait_timer():
            conn = stack.enter_context(pool().connection())
        DB_CONNECTIONS_IN_USE.inc()
        try:
            yield conn
        finally:
            DB_CONNECTIONS_IN_USE.dec()


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


_NO_ROW = object()


class StreamedRows:
    """Rows from a query executed as a plain streamed statement.

    Deliberately not a named (DECLARE'd) cursor, though one would also keep
    memory flat: PostgreSQL never parallelises a cursor's query, and
    ``cursor_tuple_fraction`` biases the planner toward fast-start plans on
    the assumption that the client will stop reading early — an assumption
    that is always false here, because the service reads every result to
    MAXREC + 1.

    ``Cursor.stream()`` keeps the flat memory profile — rows arrive in
    server-side chunks and are yielded one at a time, and psycopg reads the
    socket only when the consumer asks for more, so a slow reader stalls the
    server through TCP backpressure instead of buffering — while the
    statement is planned as what it is: a plain query, read to the end, free
    to use parallel workers.

    Construction sends the query and waits for the first chunk, so the
    cursor's ``description`` is populated before any row is consumed, for an
    empty result too. The connection is busy until the rows are exhausted or
    ``close()`` is called; close cancels the statement and drains what
    already arrived, so an abandoned stream (MAXREC overflow, a client that
    disconnected mid-download) hands back a reusable connection. Callers
    wrap it in ``contextlib.closing``.

    ``statement_timeout`` bounds the whole statement, production and delivery
    both. For a service timeout that is the honest meaning — "the sync query
    may take this long" — rather than a bound on each internal fetch.
    """

    def __init__(self, cur, sql: str, chunk_rows: int):
        # chunked retrieval needs libpq 17+; older builds fall back to
        # row-by-row, which is correct and merely chattier
        size = chunk_rows if psycopg.capabilities.has_stream_chunked() else 1
        self._gen = cur.stream(sql, size=size)
        self._first = next(self._gen, _NO_ROW)
        if self._first is _NO_ROW and cur.description is None:
            # A zero-row stream finishes without ever exposing the row
            # description, and an empty result still needs its columns — an
            # empty VOTable carries its FIELDs. Recover them with a LIMIT 0
            # probe, which parses and plans but never pulls a row from its
            # child plan, so the query's work is not paid twice.
            probe = sql.rstrip().rstrip(";")
            cur.execute(f"SELECT * FROM ({probe}) AS empty_result LIMIT 0")

    def __iter__(self):
        if self._first is not _NO_ROW:
            first, self._first = self._first, _NO_ROW
            yield first
        yield from self._gen

    def close(self) -> None:
        self._gen.close()
