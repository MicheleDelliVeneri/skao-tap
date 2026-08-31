"""Startup schema handling shared by both services, and the explicit
pre-deploy bootstrap command.

By default every service replica ensures the schema at startup (idempotent,
advisory-locked DDL), which is what a rolling upgrade relies on: whichever
new pod touches the database first migrates it forward. A deployment that
would rather keep DDL away from the runtime credentials runs

    python -m egernia_core.bootstrap

once against the database before deploying (with credentials that may own
schema changes) and sets TAP_SCHEMA_BOOTSTRAP_ON_STARTUP=false, after which
the services only *verify* the schema at startup and fail fast — naming this
command — when it is missing or outdated.
"""

import logging
import time

from . import uws
from .config import settings
from .db import close_pool
from .db import connection as db_connection
from .metadata import ingest
from .metadata.plugins import MetadataPlugin

log = logging.getLogger("egernia_core")

BOOTSTRAP_COMMAND = "python -m egernia_core.bootstrap"


def bootstrap(conn, plugins: list[MetadataPlugin]) -> None:
    """All the DDL the services need: the uws.jobs forward migration plus
    each plugin's generated tables and TAP_SCHEMA registration."""
    uws.ensure_job_columns(conn)
    for plugin in plugins:
        ingest.ensure_schema(conn, plugin)


def check_ready(conn, plugins: list[MetadataPlugin]) -> None:
    """Read-only verification that a bootstrap already ran.

    Selects every column the services rely on (zero rows), so a missing
    table and a table an older bootstrap left without a newer model's
    columns both fail here — at startup, with the remedy named — rather
    than later as a 500 on the endpoints that touch them.
    """
    try:
        conn.execute(f"SELECT {uws.JOB_COLUMNS_SQL} FROM uws.jobs WHERE false")
        for plugin in plugins:
            for table in plugin.tables:
                columns = ", ".join(c.name for c in table.columns)
                conn.execute(f"SELECT {columns} FROM {table.qualified} WHERE false")
    except Exception as exc:
        raise RuntimeError(
            "schema is missing or outdated and startup DDL is disabled"
            f" (TAP_SCHEMA_BOOTSTRAP_ON_STARTUP=false); run `{BOOTSTRAP_COMMAND}`"
            " against this database before deploying"
        ) from exc


def startup(plugins: list[MetadataPlugin], attempts: int = 5, delay_s: float = 2.0) -> None:
    """Prepare — or, with startup DDL disabled, verify — the schema at
    service start, retrying while the database comes up."""
    if settings.schema_bootstrap_on_startup:
        step, what = bootstrap, "bootstrap"
    else:
        step, what = check_ready, "readiness check"
        log.info("startup schema DDL disabled; verifying a pre-deploy bootstrap ran")
    _run_with_retry(step, plugins, attempts, delay_s, what)


def _run_with_retry(step, plugins, attempts: int, delay_s: float, what: str) -> None:
    for attempt in range(1, attempts + 1):
        try:
            with db_connection() as conn, conn.transaction():
                step(conn, plugins)
            return
        except Exception as exc:
            if attempt == attempts:
                # Fail fast: a half-initialized service would only surface
                # confusing errors later on the metadata endpoints and
                # generated-schema queries; the orchestrator should restart us.
                raise RuntimeError(f"schema {what} failed after {attempts} attempts") from exc
            log.warning("schema %s attempt %d failed (%s), retrying", what, attempt, exc)
            time.sleep(delay_s)


def main() -> None:
    """The explicit pre-deploy bootstrap: ensure everything, once, and exit."""
    from .metadata.plugins import active_plugins
    from .observability import configure_logging

    configure_logging("schema-bootstrap")
    plugins = active_plugins()
    log.info("bootstrapping schema (plugins: %s)", ", ".join(p.name for p in plugins) or "none")
    try:
        # generous retries: a fresh environment's database may still be starting
        _run_with_retry(bootstrap, plugins, attempts=30, delay_s=2.0, what="bootstrap")
        with db_connection() as conn:
            ingest.warn_tap_schema_divergence(conn)
    finally:
        close_pool()
    log.info("schema bootstrap complete")


if __name__ == "__main__":
    main()
