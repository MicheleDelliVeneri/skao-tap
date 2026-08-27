"""What server-side DSV costs, once the bytes are made to match.

#106 proposes replacing the Python DSV writer with `COPY ... FORMAT csv` to
recover the 3.90 ms/request that the result writers and psycopg row conversion
cost together. `test_copy_dsv_differential` established that raw `COPY` does not
produce the same bytes — float8 in particular diverges four ways, and float8 is
most of ObsCore.

So the honest comparison is not "writer against COPY" but "writer against COPY
*plus whatever makes the bytes match*". This measures all three, because a COPY
that needs per-value server-side formatting may cost more than the Python it
replaces, and that decides the package.

Not a regression gate: no threshold is asserted, because the number belongs to
whatever host it runs on. It prints a table and asserts only that the three
paths agree on row count and that the formatted path really does match the
writer byte for byte — the two things that would invalidate the comparison.

Read it with:

    uv run pytest tests/component/test_copy_dsv_cost.py -s
"""

from __future__ import annotations

import os
import time

import psycopg
import pytest
from egernia_core.query.results import RowLimiter, columns_from_cursor, stream_dsv

pytestmark = pytest.mark.component

REPEATS = 3

# The float8 columns of ObsCore, which is where the divergence lives.
FLOAT_COLUMNS = [
    "s_ra",
    "s_dec",
    "s_fov",
    "em_min",
    "em_max",
    "t_min",
    "t_max",
    "t_exptime",
]
OTHER_COLUMNS = ["obs_id", "dataproduct_type", "obs_collection", "calib_level"]


def _float_matching_python(col: str) -> str:
    """SQL rendering a float8 the way CPython's `repr` does.

    `col::text` already agrees for every non-integral finite value: PostgreSQL 12
    and later default `extra_float_digits` to shortest-round-trip, which is what
    Python's repr produces. Only three cases need patching, and `to_char` is not
    among the options — no fixed format reproduces shortest-round-trip, so it
    cannot close this divergence at any price.

    NaN compares equal to itself in PostgreSQL, which is what makes the first
    branch work.
    """
    return f"""CASE
        WHEN {col} IS NULL THEN NULL
        WHEN {col} = 'NaN'::float8 THEN 'nan'
        WHEN {col} = 'Infinity'::float8 THEN 'inf'
        WHEN {col} = '-Infinity'::float8 THEN '-inf'
        WHEN {col} = trunc({col}) AND abs({col}) < 1e16 THEN {col}::text || '.0'
        ELSE {col}::text
    END AS {col}"""


def _plain_sql(limit: int) -> str:
    cols = ", ".join(OTHER_COLUMNS + FLOAT_COLUMNS)
    return f"SELECT {cols} FROM ivoa.obscore LIMIT {limit}"


def _formatted_sql(limit: int) -> str:
    cols = ", ".join(OTHER_COLUMNS + [_float_matching_python(c) for c in FLOAT_COLUMNS])
    return f"SELECT {cols} FROM ivoa.obscore LIMIT {limit}"


def _time_writer(conn: psycopg.Connection, sql: str) -> tuple[float, bytes]:
    """psycopg row conversion plus the Python csv.writer — today's cost."""
    started = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = columns_from_cursor(cur.description, {})
        rows = RowLimiter(cur.fetchall(), maxrec=10_000_000)
        body = b"".join(stream_dsv(columns, rows, delimiter=","))
    return time.perf_counter() - started, body


def _time_copy(conn: psycopg.Connection, sql: str) -> tuple[float, bytes]:
    started = time.perf_counter()
    chunks: list[bytes] = []
    with (
        conn.cursor() as cur,
        cur.copy(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER)") as copy,
    ):
        for chunk in copy:
            chunks.append(bytes(chunk))
    return time.perf_counter() - started, b"".join(chunks)


def _best_of(fn, conn, sql, repeats=REPEATS) -> tuple[float, bytes]:
    """Fastest of N. The floor is the signal; the rest is the machine."""
    best, body = None, b""
    for _ in range(repeats):
        elapsed, body = fn(conn, sql)
        best = elapsed if best is None else min(best, elapsed)
    return best, body


@pytest.fixture(scope="module")
def conn(database_url):
    """A connection to something holding a seeded ObsCore.

    The component suite builds an empty database per run, which has the schema
    but no rows — fine for correctness, useless for pricing a per-row cost. So
    EGERNIA_COST_DSN points this at a seeded one (docker-compose, or a
    port-forwarded deployment) and it skips without.
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
    return min(n, 20_000)


def test_price_the_paths(conn, row_count):
    """Writer, raw COPY, and COPY with byte-matching float formatting."""
    plain, formatted = _plain_sql(row_count), _formatted_sql(row_count)

    writer_s, writer_body = _best_of(_time_writer, conn, plain)
    copy_s, copy_body = _best_of(_time_copy, conn, plain)
    fmt_s, fmt_body = _best_of(_time_copy, conn, formatted)

    per_row = lambda s: s / row_count * 1e6  # noqa: E731 - µs/row, read once

    print(f"\n{'':<34}{'total ms':>10}{'µs/row':>10}{'vs writer':>12}")
    print("-" * 66)
    for label, seconds in (
        ("python writer (today)", writer_s),
        ("COPY, raw (bytes differ)", copy_s),
        ("COPY + float formatting", fmt_s),
    ):
        ratio = "—" if seconds == writer_s else f"{writer_s / seconds:.2f}x"
        print(f"{label:<34}{seconds * 1000:>10.1f}{per_row(seconds):>10.2f}{ratio:>12}")
    print(f"\nrows: {row_count}, float8 columns formatted: {len(FLOAT_COLUMNS)}")
    print(f"body bytes: writer {len(writer_body)}, raw {len(copy_body)}, formatted {len(fmt_body)}")

    # The comparison is only meaningful if the paths did the same work.
    assert writer_body.count(b"\n") == copy_body.count(b"\n") == fmt_body.count(b"\n")

    # And the formatted path has to actually close the divergence, or its cost
    # is being compared against the wrong thing.
    if fmt_body != writer_body:
        first = next(
            (i for i, (a, b) in enumerate(zip(fmt_body, writer_body, strict=False)) if a != b),
            0,
        )
        window = slice(max(0, first - 60), first + 60)
        print("\nformatted COPY still differs from the writer:")
        print(f"  writer:    ...{writer_body[window]!r}...")
        print(f"  formatted: ...{fmt_body[window]!r}...")
    assert fmt_body == writer_body, (
        "the float formatting does not reproduce the writer's bytes, so the "
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
    (i %% 360)::float8 AS s_ra,
    (i %% 90)::float8 AS s_dec,
    1.0::float8 AS s_fov,
    2.0::float8 AS em_min,
    3.0::float8 AS em_max,
    60000.0::float8 AS t_min,
    60001.0::float8 AS t_max,
    600.0::float8 AS t_exptime
FROM generate_series(1, %(rows)s) AS i
"""


def _integral_sql(rows: int, formatted: bool) -> str:
    """The all-integral corpus, optionally re-projected through the CASE."""
    base = INTEGRAL_HEAVY_SQL.replace("%(rows)s", str(rows)).replace("%%", "%")
    if not formatted:
        return base
    columns = ", ".join(
        OTHER_COLUMNS + [_float_matching_python(c) for c in FLOAT_COLUMNS]
    )
    return f"SELECT {columns} FROM ({base}) AS src"


def test_price_the_worst_case_corpus(conn):
    """Every float integral: the branch that patches them fires on every value."""
    rows = 12_800
    plain = _integral_sql(rows, formatted=False)
    formatted = _integral_sql(rows, formatted=True)

    writer_s, writer_body = _best_of(_time_writer, conn, plain)
    copy_s, _ = _best_of(_time_copy, conn, plain)
    fmt_s, fmt_body = _best_of(_time_copy, conn, formatted)

    print(f"\n{'ALL-INTEGRAL FLOATS':<34}{'total ms':>10}{'µs/row':>10}{'vs writer':>12}")
    print("-" * 66)
    for label, seconds in (
        ("python writer (today)", writer_s),
        ("COPY, raw (bytes differ)", copy_s),
        ("COPY + float formatting", fmt_s),
    ):
        ratio = "—" if seconds == writer_s else f"{writer_s / seconds:.2f}x"
        print(f"{label:<34}{seconds * 1000:>10.1f}{seconds / rows * 1e6:>10.2f}{ratio:>12}")

    assert fmt_body == writer_body, (
        "the float formatting does not reproduce the writer's bytes on an "
        "integral-heavy corpus, so its cost here is not comparable"
    )
