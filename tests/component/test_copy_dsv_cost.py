"""What server-side DSV costs, now that the bytes match.

#106 replaces the Python DSV writer with `COPY ... FORMAT csv` to recover the
3.90 ms/request the result writers and psycopg row conversion cost together.
`test_copy_dsv_differential` established that raw `COPY` does not produce the
same bytes — float8 alone diverged four ways, and float8 is most of ObsCore — so
`copy_dsv.projection` re-renders every column, and the honest comparison is not
"writer against COPY" but "writer against the projection that makes the bytes
match".

Both are measured here, against the projection the service actually uses rather
than a hand-written approximation of it, so the number cannot drift away from
the code. Raw `COPY` is measured too, as the floor: it says how much of the gap
is the server's own work and how much is the formatting.

Not a regression gate — no threshold is asserted, because the number belongs to
whatever host it runs on. It prints a table and asserts only the two things that
would invalidate the comparison: that the paths delivered the same rows, and
that the projected path really is byte-for-byte the writer's output.

Read it with:

    uv run pytest tests/component/test_copy_dsv_cost.py -s
"""

from __future__ import annotations

import os
import time

import psycopg
import pytest
from egernia_core.query.copy_dsv import (
    CopiedRows,
    header,
    projection,
    unescape,
    votable_projection,
)
from egernia_core.query.results import (
    RowLimiter,
    columns_from_cursor,
    stream_dsv,
    stream_votable,
    votable_head,
    votable_tail,
)

pytestmark = pytest.mark.component

REPEATS = 3

# The float8 columns of ObsCore, which is where the rendering work lands, plus
# the mixed kinds around them so the projection is priced on a realistic row.
COLUMNS = [
    "obs_id",
    "dataproduct_type",
    "obs_collection",
    "calib_level",
    "s_ra",
    "s_dec",
    "s_fov",
    "em_min",
    "em_max",
    "t_min",
    "t_max",
    "t_exptime",
]


def _sql(limit: int) -> str:
    # From the sample `row_count` materialised, in a fixed order: the two paths
    # are compared byte for byte, so they have to see the same rows in the
    # same order, which `LIMIT` over the ObsCore view's joins does not promise
    # -- the plan differs between a SELECT and the same query under COPY. A
    # 20,000-row sort costs both paths the same few milliseconds.
    return f"SELECT {', '.join(COLUMNS)} FROM cost_sample ORDER BY obs_publisher_did LIMIT {limit}"


def _time_writer(conn, sql: str) -> tuple[float, bytes]:
    """psycopg row conversion plus the Python csv.writer — the old path."""
    started = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = columns_from_cursor(cur.description, {})
        rows = RowLimiter(cur.fetchall(), maxrec=10_000_000)
        body = b"".join(stream_dsv(columns, rows, delimiter=","))
    return time.perf_counter() - started, body


def _time_copy(conn, sql: str, *, project: bool) -> tuple[float, bytes]:
    """The server path: `project=False` is raw COPY, the floor."""
    started = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM ({sql}) AS probe LIMIT 0")
        columns = columns_from_cursor(cur.description, {})
        statement = projection(cur.description, sql) if project else sql
    rows = CopiedRows(10_000_000)
    with (
        conn.cursor() as cur,
        cur.copy(f"COPY ({statement}) TO STDOUT WITH (FORMAT csv, DELIMITER ',')") as copy,
    ):
        body = header(columns, ",") + b"".join(rows.chunks(copy))
    return time.perf_counter() - started, body


def _time_votable_writer(conn, sql: str) -> tuple[float, bytes]:
    """psycopg row conversion plus `stream_votable` — VOTable's old path."""
    started = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = columns_from_cursor(cur.description, {})
        rows = RowLimiter(cur.fetchall(), maxrec=10_000_000)
        body = b"".join(stream_votable(columns, rows))
    return time.perf_counter() - started, body


def _time_votable_copy(conn, sql: str) -> tuple[float, bytes]:
    """The server path for VOTable: envelope here, `<TR>` rows by COPY."""
    started = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM ({sql}) AS probe LIMIT 0")
        columns = columns_from_cursor(cur.description, {})
        statement = votable_projection(cur.description, sql)
    rows = CopiedRows(10_000_000)
    with conn.cursor() as cur, cur.copy(f"COPY ({statement}) TO STDOUT WITH (FORMAT text)") as copy:
        body = (
            votable_head(columns)
            + b"".join(unescape(chunk) for chunk in rows.chunks(copy))
            + votable_tail(rows.overflowed)
        )
    return time.perf_counter() - started, body


def _best_of(fn, *args, **kwargs) -> tuple[float, bytes]:
    """Fastest of N. The floor is the signal; the rest is the machine."""
    best, body = None, b""
    for _ in range(REPEATS):
        elapsed, body = fn(*args, **kwargs)
        best = elapsed if best is None else min(best, elapsed)
    return best, body


def _report(label: str, rows: int, timings: list[tuple[str, float]]) -> None:
    baseline = timings[0][1]
    print(f"\n{label:<34}{'total ms':>10}{'µs/row':>10}{'vs writer':>12}")
    print("-" * 66)
    for name, seconds in timings:
        ratio = "—" if seconds == baseline else f"{baseline / seconds:.2f}x"
        print(f"{name:<34}{seconds * 1000:>10.1f}{seconds / rows * 1e6:>10.2f}{ratio:>12}")


@pytest.fixture(scope="module")
def conn(database_url):
    """A connection to something holding a seeded ObsCore.

    The component suite builds an empty database per run, which has the schema
    but no rows — fine for correctness, useless for pricing a per-row cost. So
    EGERNIA_COST_DSN points this at a seeded one (docker-compose's, or a
    port-forward to a deployment) and it skips without.
    """
    dsn = os.environ.get("EGERNIA_COST_DSN", database_url)
    with psycopg.connect(dsn) as connection:
        yield connection


@pytest.fixture(scope="module")
def row_count(conn):
    present = conn.execute("SELECT to_regclass('ivoa.obscore') IS NOT NULL").fetchone()[0]
    if not present:
        pytest.skip(
            "no ivoa.obscore here. Point EGERNIA_COST_DSN at a seeded database "
            "(docker-compose's, or a port-forward to a deployment)."
        )
    n = conn.execute("SELECT count(*) FROM ivoa.obscore").fetchone()[0]
    if n < 1000:
        pytest.skip(
            f"ivoa.obscore holds {n} rows: too few to price a per-row cost. "
            "Seed it first (python -m egernia_dataset.seed)."
        )
    n = min(n, 20_000)
    conn.execute(
        f"CREATE TEMP TABLE cost_sample AS SELECT obs_publisher_did, {', '.join(COLUMNS)}"
        f" FROM ivoa.obscore LIMIT {n}"
    )
    return n


def test_price_the_paths(conn, row_count):
    """Writer, raw COPY, and the projection the service actually ships."""
    sql = _sql(row_count)

    writer_s, writer_body = _best_of(_time_writer, conn, sql)
    raw_s, raw_body = _best_of(_time_copy, conn, sql, project=False)
    projected_s, projected_body = _best_of(_time_copy, conn, sql, project=True)

    _report(
        "SEEDED OBSCORE",
        row_count,
        [
            ("python writer (before)", writer_s),
            ("COPY, raw (wrong bytes)", raw_s),
            ("COPY + projection (after)", projected_s),
        ],
    )
    print(f"\nrows: {row_count}, columns: {len(COLUMNS)}")
    print(f"body bytes: writer {len(writer_body)}, projected {len(projected_body)}")

    # The comparison is only meaningful if the paths did the same work.
    assert writer_body.count(b"\n") == raw_body.count(b"\n") == projected_body.count(b"\n")
    assert projected_body == writer_body, (
        "the projection does not reproduce the writer's bytes here, so the "
        "timings above compare a fast path against a wrong one"
    )


# The corpus matters. The seeded generator produces continuous random floats, so
# the branch that patches integral values almost never fires — 0 of 12,800 s_ra
# values are integral. A real archive holds round numbers (exposure times,
# channel counts, coordinates entered by hand), and every one of them takes the
# concatenation branch. This bounds that end.
INTEGRAL_HEAVY_SQL = """
SELECT
    'obs-' || i AS obs_id,
    'image' AS dataproduct_type,
    'SKAO/SKA-Mid' AS obs_collection,
    2 AS calib_level,
    (i % 360)::float8 AS s_ra,
    (i % 90)::float8 AS s_dec,
    1.0::float8 AS s_fov,
    2.0::float8 AS em_min,
    3.0::float8 AS em_max,
    60000.0::float8 AS t_min,
    60001.0::float8 AS t_max,
    600.0::float8 AS t_exptime
FROM generate_series(1, {rows}) AS i
"""


def test_price_the_worst_case_corpus(conn):
    """Every float integral: the branch that patches them fires on every value."""
    rows = 12_800
    sql = INTEGRAL_HEAVY_SQL.format(rows=rows)

    writer_s, writer_body = _best_of(_time_writer, conn, sql)
    raw_s, _ = _best_of(_time_copy, conn, sql, project=False)
    projected_s, projected_body = _best_of(_time_copy, conn, sql, project=True)

    _report(
        "ALL-INTEGRAL FLOATS",
        rows,
        [
            ("python writer (before)", writer_s),
            ("COPY, raw (wrong bytes)", raw_s),
            ("COPY + projection (after)", projected_s),
        ],
    )

    assert projected_body == writer_body, (
        "the projection does not reproduce the writer's bytes on an "
        "integral-heavy corpus, so its cost here is not comparable"
    )


def test_price_the_votable_paths(conn, row_count):
    """VOTable, the TAP default: `stream_votable` against the folded projection."""
    sql = _sql(row_count)

    writer_s, writer_body = _best_of(_time_votable_writer, conn, sql)
    copied_s, copied_body = _best_of(_time_votable_copy, conn, sql)

    _report(
        "SEEDED OBSCORE, VOTABLE",
        row_count,
        [("python writer (before)", writer_s), ("COPY + projection (after)", copied_s)],
    )
    print(f"\nrows: {row_count}, columns: {len(COLUMNS)}")
    print(f"body bytes: writer {len(writer_body)}, projected {len(copied_body)}")

    assert copied_body == writer_body, (
        "the VOTable projection does not reproduce the writer's bytes here, so the "
        "timings above compare a fast path against a wrong one"
    )
