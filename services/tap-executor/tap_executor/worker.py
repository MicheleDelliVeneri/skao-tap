"""UWS job executor.

Polls uws.jobs for QUEUED jobs (claiming with FOR UPDATE SKIP LOCKED so
multiple executor replicas can run side by side), executes the translated
PostgreSQL query under a read-only role and per-job statement timeout,
writes the serialized result to the shared results volume, and finalizes
the job phase. Also garbage-collects jobs past their destruction time.
"""

import datetime
import logging
import os
import shutil
import time

from tapcore import uws
from tapcore.adql import apply_maxrec, touched_tables
from tapcore.config import settings
from tapcore.db import pool
from tapcore.results import RowLimiter, columns_from_cursor, stream, tap_schema_metadata
from tapcore.upload import (
    create_upload_tables,
    load_uploads,
    parse_upload_param,
    rewrite_upload_refs,
)
from tapcore.votable import normalize_format

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tap-executor")

POLL_INTERVAL_S = 1.0
CLEANUP_INTERVAL_S = 60.0

CLAIM_SQL = f"""
UPDATE uws.jobs
   SET phase = 'EXECUTING', start_time = now()
 WHERE job_id = (
        SELECT job_id FROM uws.jobs
         WHERE phase = 'QUEUED'
         ORDER BY creation_time
         FOR UPDATE SKIP LOCKED
         LIMIT 1)
RETURNING {uws.JOB_COLUMNS}
"""


def _now():
    return datetime.datetime.now(datetime.UTC)


def claim_job() -> dict | None:
    with pool().connection() as conn:
        row = conn.execute(CLAIM_SQL).fetchone()
    if row is None:
        return None
    return uws._row_to_job(row)


def execute_job(job: dict) -> None:
    job_id = job["job_id"]
    params = job["parameters"] or {}
    log.info("executing job %s", job_id)
    try:
        maxrec = min(int(params.get("MAXREC", settings.default_maxrec)), settings.hard_maxrec)
        fmt_key, mime, ext = normalize_format(params.get("RESPONSEFORMAT") or params.get("FORMAT"))
        sql = apply_maxrec(job["query_sql"], maxrec)

        uploads = []
        if params.get("UPLOAD"):
            names = [name for name, _ in parse_upload_param(params["UPLOAD"])]
            uploads = load_uploads(
                job_id, names, settings.upload_max_rows, settings.upload_max_bytes
            )
            sql = rewrite_upload_refs(sql, {u.name for u in uploads})

        result_dir = uws.job_results_dir(job_id)
        os.makedirs(result_dir, exist_ok=True)
        result_path = os.path.join(result_dir, f"result.{ext}")

        # stream from a server-side cursor straight into the result file, so
        # large result sets are never materialized in memory
        result_size = 0
        with pool().connection() as conn, conn.transaction():
            tap_meta = tap_schema_metadata(conn, touched_tables(job["query_sql"]))
            if uploads:
                create_upload_tables(conn, uploads, settings.query_role)
            timeout_ms = int(job["execution_duration"]) * 1000
            if timeout_ms > 0:
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(timeout_ms),),
                )
            # publish this backend's PID so ABORT can pg_cancel_backend() it
            pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            with pool().connection() as side:
                uws.update_job(side, job_id, backend_pid=pid)
            conn.execute(f"SET LOCAL ROLE {settings.query_role}")
            with conn.cursor(name=f"tap_job_{job_id}") as cur:
                cur.itersize = 5000
                cur.execute(sql)
                columns = columns_from_cursor(cur.description, tap_meta)
                limiter = RowLimiter(cur, maxrec)
                with open(result_path, "wb") as fh:
                    for chunk in stream(columns, limiter, fmt_key):
                        result_size += fh.write(chunk)

        with pool().connection() as conn:
            current = uws.get_job(conn, job_id)
            if current["phase"] == "ABORTED":  # aborted while running
                shutil.rmtree(result_dir, ignore_errors=True)
                return
            uws.update_job(
                conn,
                job_id,
                phase="COMPLETED",
                end_time=_now(),
                result_mime=mime,
                result_size=result_size,
                backend_pid=None,
            )
        log.info("job %s completed (%d rows, %s)", job_id, limiter.count, limiter.status)
    except Exception as exc:
        shutil.rmtree(uws.job_results_dir(job_id), ignore_errors=True)
        with pool().connection() as conn:
            current = uws.get_job(conn, job_id)
            if current["phase"] == "ABORTED":
                # ABORT cancelled our statement mid-run: not an error
                log.info("job %s aborted while executing", job_id)
                return
            log.exception("job %s failed", job_id)
            uws.update_job(
                conn,
                job_id,
                phase="ERROR",
                end_time=_now(),
                error_type="fatal",
                error_message=str(exc)[:4000],
                backend_pid=None,
            )


def cleanup_expired() -> None:
    with pool().connection() as conn:
        rows = conn.execute(
            "DELETE FROM uws.jobs WHERE destruction < now() RETURNING job_id"
        ).fetchall()
    for (job_id,) in rows:
        shutil.rmtree(uws.job_results_dir(job_id), ignore_errors=True)
        log.info("destroyed expired job %s", job_id)


def _ensure_backend_pid_column(attempts: int = 30, delay_s: float = 2.0) -> None:
    """Forward-migrate uws.jobs for deployments that predate ABORT support;
    also what CLAIM_SQL needs, so retry until the database is reachable."""
    for attempt in range(1, attempts + 1):
        try:
            with pool().connection() as conn:
                conn.execute("ALTER TABLE uws.jobs ADD COLUMN IF NOT EXISTS backend_pid integer")
            return
        except Exception as exc:
            if attempt == attempts:
                raise
            log.warning("uws.jobs migration attempt %d failed (%s), retrying", attempt, exc)
            time.sleep(delay_s)


def main() -> None:
    log.info("tap-executor started (results dir: %s)", settings.results_dir)
    os.makedirs(settings.results_dir, exist_ok=True)
    _ensure_backend_pid_column()
    last_cleanup = 0.0
    while True:
        # survive transient failures (e.g. an ABORT's pg_cancel_backend
        # racing a finished execution and cancelling a pooled connection)
        try:
            job = claim_job()
            if job is not None:
                execute_job(job)
                continue
            if time.monotonic() - last_cleanup > CLEANUP_INTERVAL_S:
                cleanup_expired()
                last_cleanup = time.monotonic()
        except Exception:
            log.exception("executor loop error, retrying")
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
