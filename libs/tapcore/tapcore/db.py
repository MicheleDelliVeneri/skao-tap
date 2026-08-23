"""PostgreSQL access via a shared psycopg3 connection pool."""

from psycopg_pool import ConnectionPool

from .config import settings

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


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
