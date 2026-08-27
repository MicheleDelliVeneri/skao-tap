"""The four contracts #106 said had to survive moving DSV into the server.

Byte equality is the fifth and is asked in `test_copy_dsv_differential`. These
are the ones about behaviour rather than bytes, each demonstrated against a real
PostgreSQL rather than argued:

- **DALI overflow.** `RowLimiter` yields at most `maxrec` and proves overflow by
  reading one more. `COPY` delivers opaque bytes, so the count and the extra row
  had to be recovered some other way; that they still come out right at exactly
  `maxrec` and at `maxrec + 1` is what these tests pin.
- **Streaming and backpressure.** Package 11 moved results onto `Cursor.stream()`
  to keep memory flat against a slow reader. `copy()` has to preserve it.
- **`syncTimeoutSeconds`.** Package 11 made it bound the whole statement. It has
  to still bound the whole `COPY`, delivery included, not just the planning.
- **Type coercion**, which is the differential file's subject, appears here only
  as the decline: a result the projection cannot promise falls back to the
  Python writer and says so in a counter.
"""

from __future__ import annotations

import tracemalloc

import psycopg
import pytest
from egernia_core.query.copy_dsv import COPY_DSV_FALLBACKS, COPY_DSV_RESULTS, result_stream
from egernia_core.query.results import RowLimiter, columns_from_cursor, stream_dsv

pytestmark = pytest.mark.component

ROWS_SQL = "SELECT i::int4 AS n, ('r' || i)::text AS t FROM generate_series(1, %d) AS i"


@pytest.fixture
def conn(database_url):
    with psycopg.connect(database_url) as connection:
        yield connection


def _counter(counter, **labels) -> float:
    """One Prometheus counter's current value, or 0 if never incremented."""
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and sample.labels == labels:
                return sample.value
    return 0.0


def _served(conn, sql: str, fmt_key: str, maxrec: int):
    """(body, accounting) through the same entry point the services use."""
    with conn.cursor() as cur, result_stream(cur, sql, {}, fmt_key, maxrec, 5000) as (chunks, rows):
        return b"".join(chunks), rows


# --- overflow ---------------------------------------------------------------


@pytest.mark.parametrize(("total", "maxrec"), [(0, 10), (1, 10), (9, 10), (10, 10), (11, 10)])
def test_overflow_and_count_match_the_python_writer(conn, total, maxrec):
    """At `maxrec` and at `maxrec + 1`, both paths must agree on both facts.

    `maxrec` exactly is the case worth having: a result that fills the limit is
    not truncated, and reporting OVERFLOW for it would send clients back to
    re-query something they already have in full.
    """
    # what the service asks for: apply_maxrec fetches one row past the limit
    sql = f"SELECT * FROM ({ROWS_SQL % total}) AS s LIMIT {maxrec + 1}"

    with conn.cursor() as cur:
        cur.execute(sql)
        columns = columns_from_cursor(cur.description, {})
        limiter = RowLimiter(cur.fetchall(), maxrec)
        expected = b"".join(stream_dsv(columns, limiter, ","))

    body, rows = _served(conn, sql, "csv", maxrec)

    assert (rows.count, rows.status) == (limiter.count, limiter.status)
    assert body == expected
    assert body.count(b"\n") == min(total, maxrec) + 1  # + the header


def test_the_row_past_maxrec_is_read_and_not_sent(conn):
    """The extra row must leave the connection usable.

    Walking away from an unfinished COPY would hand a connection back to the
    pool still in COPY state, which is a failure the next borrower pays for.
    Reading the one extra row is how this path avoids it, so the check is that
    the same connection answers afterwards.
    """
    sql = f"SELECT * FROM ({ROWS_SQL % 50}) AS s LIMIT 11"
    body, rows = _served(conn, sql, "csv", 10)
    assert (rows.count, rows.overflowed) == (10, True)
    assert b"r11" not in body
    assert conn.execute("SELECT 1").fetchone() == (1,)


# --- the path actually taken ------------------------------------------------


@pytest.mark.parametrize("fmt_key", ["csv", "tsv"])
def test_dsv_takes_the_server_side_path(conn, fmt_key):
    """Not that it produces the right bytes — that it is the path being used.

    Every other test here would pass with the COPY path silently declining
    everything, because the fallback is by construction correct.
    """
    before = _counter(COPY_DSV_RESULTS)
    _served(conn, ROWS_SQL % 5, fmt_key, 100)
    assert _counter(COPY_DSV_RESULTS) == before + 1


@pytest.mark.parametrize("fmt_key", ["votable", "json"])
def test_other_formats_are_untouched(conn, fmt_key):
    """This package is DSV only. A VOTable request must not even be considered
    for COPY, and so must not be counted as a fallback either."""
    before = _counter(COPY_DSV_RESULTS), _counter(COPY_DSV_FALLBACKS, reason="unknown_type")
    body, rows = _served(conn, ROWS_SQL % 3, fmt_key, 100)
    assert rows.count == 3
    assert body
    assert (
        _counter(COPY_DSV_RESULTS),
        _counter(COPY_DSV_FALLBACKS, reason="unknown_type"),
    ) == before


def test_an_undecidable_result_falls_back_and_is_counted(conn):
    """A `bytea` column has no COPY rendering, so the writer serves the result
    and the counter says which reason sent it there."""
    before = _counter(COPY_DSV_FALLBACKS, reason="unknown_type")
    sql = "SELECT 1::int4 AS n, '\\x0001'::bytea AS b"

    body, rows = _served(conn, sql, "csv", 100)

    assert _counter(COPY_DSV_FALLBACKS, reason="unknown_type") == before + 1
    assert rows.count == 1
    # and it is the writer's bytes, hex-encoded the way `_plain` does it
    assert body.splitlines()[1] == b"1,0001"


# --- streaming and backpressure --------------------------------------------


def test_the_working_set_stays_flat_against_a_slow_reader(conn):
    """A body far larger than memory must never be held in it.

    The reader here consumes chunk by chunk and drops each one, which is what
    an HTTP response does. If `copy()` buffered the result the peak would track
    the body; the assertion is that it tracks the chunk size instead.
    """
    rows = 120_000
    sql = f"SELECT i::int4 AS n, repeat('x', 200)::text AS t FROM generate_series(1, {rows}) AS i"

    tracemalloc.start()
    try:
        delivered = 0
        with conn.cursor() as cur, result_stream(cur, sql, {}, "csv", rows, 5000) as (chunks, acc):
            for chunk in chunks:
                delivered += len(chunk)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert acc.count == rows
    assert delivered > 24_000_000, f"only {delivered} bytes: the corpus is too small to prove this"
    # Generous by two orders of magnitude against the body, and still far under
    # anything that could be holding it: the chunk buffer is 64 KiB.
    assert peak < 4_000_000, f"peak {peak} bytes for a {delivered}-byte body is not flat"


def test_statement_timeout_bounds_the_whole_copy(conn):
    """The timeout has to cover delivery, not only planning.

    `pg_sleep` in the WHERE clause makes the query slow *per row*, so a COPY
    that had escaped `statement_timeout` once it started streaming would run to
    completion here rather than being cancelled.
    """
    conn.execute("SET statement_timeout = 500")
    sql = (
        "SELECT i::int4 AS n, 'x'::text AS t FROM generate_series(1, 20) AS i"
        " WHERE pg_sleep(0.2) IS NULL"
    )
    with pytest.raises(psycopg.errors.QueryCanceled):
        _served(conn, sql, "csv", 1000)
