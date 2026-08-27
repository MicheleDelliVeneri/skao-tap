"""Populate a deployed service's database with queryable ODP and software data.

The same generator the benchmark suite uses, pointed at a live deployment
rather than a benchmark cluster, so a seeded environment and a published
measurement describe the same schema and the same row shapes.

Run as the post-deploy step of a deployment:

    TAP_DATABASE_URL=postgresql://... python -m egernia_bench.dataset.seed

Row-driven, where the suite's own generation is size-driven. A benchmark cares
what 10 GiB of database does to a query plan; a seeded environment cares that
there are enough rows to ask interesting questions about, and a row count is
what someone asking for one can state. `grow_to` stops on
``pg_database_size()``; this stops on ``count(*)``.

Two things make it safe to run on every deploy rather than once: generation
stops once the target row count is present, and every statement is idempotent
on its primary key with generation resuming from the first incomplete project,
so a run killed halfway continues rather than duplicating.

It deliberately does not create the schema. ``srcnet.data_products`` and the
``ivoa.obscore`` view are made by the metadata plugins when the API boots, and
a seeder that defined its own would be a second definition to keep in step.
It waits for the API to have done it instead.
"""

from __future__ import annotations

import logging
import math
import os
import pathlib
import sys
import time

import psycopg
import yaml

from egernia_bench.dataset import generate

log = logging.getLogger("egernia_bench.seed")

# config/datasets.yaml, three levels up from this module inside the suite.
SUITE = pathlib.Path(__file__).resolve().parents[2]

# What the ODP plugin creates at API bootstrap. Both, because the view is what
# TAP publishes and the table is what the generator writes.
REQUIRED = ("srcnet.data_products", "ivoa.obscore")

# The model's fan-out is fixed, so a data-product count is a project count.
# One project yields 4 observations, 8 scheduling blocks, 16 execution blocks,
# 128 data products and 256 artifacts. Asking for equal row counts across the
# ODP tables is not possible: the ratios are the hierarchy.
PRODUCTS_PER_PROJECT = (
    generate.OBS_PER_PROJECT * generate.SBD_PER_OBS * generate.EB_PER_SBD * generate.PRODUCTS_PER_EB
)

# The software catalogue's uri is {publisher}:{tool}:{major}.{minor}.{patch},
# built from 5 publishers, 11 tools and a version triple derived from the row
# index modulo 4, 10 and 7 — so at most 5 * 11 * lcm(4,10,7) = 7,700 distinct
# uris exist, and ON CONFLICT (uri) DO NOTHING discards the rest. Measured
# against a real database, not just derived. Deliberate on the generator's
# part: a catalogue of tools is hundreds of rows in reality however large the
# data holdings become. Widening it means widening the vocabulary in
# generate.SOFTWARE.
SOFTWARE_URI_CEILING = 7700


def wait_for_schema(dsn: str, timeout_s: float, interval_s: float = 5.0) -> None:
    """Block until the metadata plugins have created their tables.

    The post-deploy hook runs after the rollout reports ready, which is
    normally after the API has bootstrapped — but "normally" is not "always",
    and the generator's own check raises rather than waits. A seeder that
    failed the first time a deploy was a few seconds out of order would be
    reported as a broken deployment.
    """
    deadline = time.monotonic() + timeout_s
    missing: list[str] = list(REQUIRED)
    while True:
        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
                missing = [
                    name
                    for name in REQUIRED
                    if conn.execute("SELECT to_regclass(%s) IS NULL", (name,)).fetchone()[0]
                ]
            if not missing:
                return
            log.info("waiting for %s", ", ".join(missing))
        except psycopg.OperationalError as exc:
            log.info("waiting for the database: %s", str(exc).strip())
        if time.monotonic() >= deadline:
            absent = ", ".join(missing) or "the database"
            raise SystemExit(
                f"timed out after {timeout_s:.0f}s waiting for {absent}."
                " The ODP metadata plugin creates these when the API boots, so check that"
                " tap-api is running and reached this database."
            )
        time.sleep(interval_s)


def projects_for(products: int) -> int:
    """Projects needed to reach a data-product count, rounded up."""
    return math.ceil(products / PRODUCTS_PER_PROJECT)


def grow_products_to(conn, cfg: dict, target_products: int, batch_projects: int) -> int:
    """Generate whole projects until the data-product target is reached.

    The row-driven counterpart of generate.grow_to. Kept here rather than in
    generate, because the suite's published sizes come from its size-driven
    path and there is no reason for seeding to touch it.

    Returns the highest project index written.
    """
    wanted_projects = projects_for(target_products)
    index = generate.highest_project(conn)
    if index:
        log.info("resuming generation from project %d", index)
    while index < wanted_projects:
        lo = index + 1
        hi = min(index + batch_projects, wanted_projects)
        for _level, statement in generate.LEVELS:
            with conn.cursor() as cur:
                cur.execute(statement, generate._params(cfg, lo, hi))
            conn.commit()
        index = hi
        log.info(
            "%d/%d projects (%d/%d data products)",
            index,
            wanted_projects,
            index * PRODUCTS_PER_PROJECT,
            target_products,
        )
    return index


def write_software(conn, cfg: dict, candidates: int) -> int:
    """Write the software catalogue. Returns the row count actually present.

    `candidates` is how many rows the statement generates, not how many land:
    publisher and tool are drawn pseudo-randomly per index, so covering the
    vocabulary is a coupon-collector problem rather than an enumeration.
    Measured against a real database: 7,700 candidates reach 4,910 distinct
    uris, 20,000 reach 7,120, and 100,000 saturate the ceiling at 7,700. The
    surplus is discarded by ON CONFLICT and costs seconds, so the default
    generates far more candidates than rows on purpose.
    """
    cfg = {**cfg, "generation": {**cfg["generation"], "software_packages": candidates}}
    params = generate._params(cfg, 0, 0)
    for statement in (generate.SOFTWARE, generate.SOFTWARE_ARTIFACTS):
        with conn.cursor() as cur:
            cur.execute(statement, params)
    conn.commit()
    return conn.execute("SELECT count(*) FROM srcnet.software").fetchone()[0]


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    dsn = os.environ.get("TAP_DATABASE_URL", "")
    if not dsn:
        log.error("TAP_DATABASE_URL is not set: it names the database to populate")
        return 2

    # Data products, because that table is 1:1 with ivoa.obscore and so is the
    # row count a TAP query actually meets. The other ODP levels follow from
    # the model's fan-out.
    target_products = int(os.environ.get("TARGET_PRODUCTS", "500000"))
    # Candidate rows for the software catalogue, not resulting rows: 100,000
    # candidates saturate the 7,700-uri ceiling, and the discarded surplus
    # costs seconds. Asking for 500,000 software rows is not available at all
    # without widening the vocabulary in generate.SOFTWARE.
    software_candidates = int(os.environ.get("SOFTWARE_CANDIDATES", "100000"))
    wait_s = float(os.environ.get("SCHEMA_WAIT_SECONDS", "900"))

    cfg = yaml.safe_load((SUITE / "config" / "datasets.yaml").read_text())
    batch = int(os.environ.get("BATCH_PROJECTS", str(cfg["generation"]["batch_projects"])))

    wait_for_schema(dsn, wait_s)

    wanted_projects = projects_for(target_products)
    log.info(
        "populating: %d data products (%d projects), software catalogue from %d candidates"
        " (ceiling %d distinct uris)",
        wanted_projects * PRODUCTS_PER_PROJECT,
        wanted_projects,
        software_candidates,
        SOFTWARE_URI_CEILING,
    )
    started = time.monotonic()
    with psycopg.connect(dsn, autocommit=False) as conn:
        generate.apply_schema(conn)
        software_rows = write_software(conn, cfg, software_candidates)
        # The GiST footprint indexes are dropped for the load and rebuilt
        # after: building them once at the end costs far less than maintaining
        # them per insert.
        with generate._spatial_indexes_set_aside(conn):
            projects = grow_products_to(conn, cfg, target_products, batch)
        log.info("VACUUM ANALYZE")
        with psycopg.connect(dsn, autocommit=True) as vac:
            vac.execute("VACUUM ANALYZE")
        stats = generate.collect_stats(conn, "seed", projects, time.monotonic() - started)

    for table in generate.TABLES:
        log.info("%-28s %10d rows", table, stats.row_counts.get(table, 0))
    log.info(
        "%.2f GiB total, %d software rows of a possible %d, done in %.0fs",
        stats.database_bytes / 2**30,
        software_rows,
        SOFTWARE_URI_CEILING,
        time.monotonic() - started,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
