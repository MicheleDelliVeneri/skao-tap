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
from tapcore.adql import apply_maxrec
from tapcore.config import settings
from tapcore.db import pool
from tapcore.votable import normalize_format, serialize

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

        with pool().connection() as conn, conn.transaction():
            timeout_ms = int(job["execution_duration"]) * 1000
            if timeout_ms > 0:
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(timeout_ms),),
                )
            conn.execute(f"SET LOCAL ROLE {settings.query_role}")
            cur = conn.execute(sql)
            names = [d.name for d in cur.description]
            rows = cur.fetchall()

        status = "OK"
        if len(rows) > maxrec:
            rows = rows[:maxrec]
            status = "OVERFLOW"
        body = serialize(names, rows, fmt_key, status)

        result_dir = os.path.join(settings.results_dir, job_id)
        os.makedirs(result_dir, exist_ok=True)
        result_path = os.path.join(result_dir, f"result.{ext}")
        with open(result_path, "wb") as fh:
            fh.write(body)

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
                result_size=len(body),
            )
        log.info("job %s completed (%d rows, %s)", job_id, len(rows), status)
    except Exception as exc:
        log.exception("job %s failed", job_id)
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
        shutil.rmtree(os.path.join(settings.results_dir, job_id), ignore_errors=True)
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
