"""Populate a deployed service's database with queryable ODP and software data.

The same generator the benchmark suite uses, pointed at a live deployment
rather than a benchmark cluster, so a seeded environment and a published
measurement describe the same schema and the same row shapes.

Run as the post-deploy step of a deployment:

    TAP_DATABASE_URL=postgresql://... python -m egernia_bench.dataset.seed

Two things make it safe to run on every deploy rather than once:

  - ``grow_to`` stops on ``pg_database_size()``, so a database already at the
    target is left alone — the second run costs a size query and a VACUUM.
  - every statement is idempotent on its primary key and generation resumes
    from the highest project index already present, so a run killed halfway
    continues rather than duplicating.

It deliberately does not create the schema. ``srcnet.data_products`` and the
``ivoa.obscore`` view are made by the metadata plugins when the API boots, and
a seeder that defined its own would be a second definition to keep in step.
It waits for the API to have done it instead.
"""

from __future__ import annotations

import logging
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


def targets_for(cfg: dict, target_bytes: int) -> list[dict]:
    """One target at the requested size.

    The suite's own tiers are checkpoints for a size sweep, and passing the
    ones below this size would only write their stats files on the way past.
    Seeding wants a size, so it asks for exactly that: `grow_to` reaches it in
    the same batches either way.
    """
    named = next((d for d in cfg["datasets"] if d["target_bytes"] == target_bytes), None)
    if named:
        # Reuse the suite's name when the size happens to be a known tier, so
        # the stats file is comparable with a benchmark run's.
        return [named]
    return [{"name": f"seed-{target_bytes / 2**30:.0f}GiB", "target_bytes": target_bytes}]


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    dsn = os.environ.get("TAP_DATABASE_URL", "")
    if not dsn:
        log.error("TAP_DATABASE_URL is not set: it names the database to populate")
        return 2

    target_gib = float(os.environ.get("TARGET_GIB", "20"))
    target_bytes = int(target_gib * 2**30)
    wait_s = float(os.environ.get("SCHEMA_WAIT_SECONDS", "900"))
    # Markers make a re-entered run skip a target it already finished. An
    # emptyDir is enough: losing them costs one size query, because the
    # database itself is the real checkpoint.
    markers = pathlib.Path(os.environ.get("MARKER_DIR", "/tmp/egernia-dataset"))

    # datasets.yaml parsed as-is: `generation` holds the knobs generate.build
    # reads, `datasets` the suite's size tiers. This is what the benchmark
    # runner passes too (its load_config()["datasets"]), so the seeder and a
    # benchmark run drive the generator through the same shape.
    cfg = yaml.safe_load((SUITE / "config" / "datasets.yaml").read_text())

    wait_for_schema(dsn, wait_s)

    log.info("populating to %.1f GiB (ODP hierarchy and software catalogue)", target_gib)
    started = time.monotonic()
    stats = generate.build(dsn, cfg, targets_for(cfg, target_bytes), markers)
    elapsed = time.monotonic() - started

    for stat in stats:
        log.info(
            "%s: %.2f GiB, %d ObsCore rows, %d software, %d software artifacts",
            stat.name,
            stat.database_bytes / 2**30,
            stat.obscore_rows,
            stat.row_counts.get("srcnet.software", 0),
            stat.row_counts.get("srcnet.software_artifacts", 0),
        )
    log.info("done in %.0fs", elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
