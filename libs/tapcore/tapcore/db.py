"""PostgreSQL access via a shared psycopg3 connection pool."""

import contextlib

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
    service's real backpressure signal, and it was invisible when a busy pool
    took a whole worker down with it.

    Only the acquisition is timed. Timing the whole block — which is what a
    combined ``with`` does, since the caller's work happens at the yield —
    added query and streaming time to the wait, and a sync query holds its
    connection for the length of the client's download. That turned the one
    metric that reports backpressure into a slow-response metric.
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
