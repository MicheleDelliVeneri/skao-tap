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

        result_dir = uws.job_results_dir(job_id)
        os.makedirs(result_dir, exist_ok=True)
        result_path = os.path.join(result_dir, f"result.{ext}")

        # stream from a server-side cursor straight into the result file, so
        # large result sets are never materialized in memory
        result_size = 0
        with pool().connection() as conn, conn.transaction():
            tap_meta = tap_schema_metadata(conn, touched_tables(job["query_sql"]))
            timeout_ms = int(job["execution_duration"]) * 1000
            if timeout_ms > 0:
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(timeout_ms),),
                )
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
            )
        log.info("job %s completed (%d rows, %s)", job_id, limiter.count, limiter.status)
    except Exception as exc:
        log.exception("job %s failed", job_id)
        shutil.rmtree(uws.job_results_dir(job_id), ignore_errors=True)
        with pool().connection() as conn:
            uws.update_job(
                conn,
                job_id,
                phase="ERROR",
                end_time=_now(),
                error_type="fatal",
                error_message=str(exc)[:4000],
            )


def cleanup_expired() -> None:
    with pool().connection() as conn:
        rows = conn.execute(
            "DELETE FROM uws.jobs WHERE destruction < now() RETURNING job_id"
        ).fetchall()
    for (job_id,) in rows:
        shutil.rmtree(uws.job_results_dir(job_id), ignore_errors=True)
        log.info("destroyed expired job %s", job_id)


def main() -> None:
    log.info("tap-executor started (results dir: %s)", settings.results_dir)
    os.makedirs(settings.results_dir, exist_ok=True)
    last_cleanup = 0.0
    while True:
        job = claim_job()
        if job is not None:
            execute_job(job)
            continue
        if time.monotonic() - last_cleanup > CLEANUP_INTERVAL_S:
            cleanup_expired()
            last_cleanup = time.monotonic()
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
