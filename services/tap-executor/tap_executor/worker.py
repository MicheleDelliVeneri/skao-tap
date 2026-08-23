"""UWS job executor.

Polls uws.jobs for QUEUED jobs (claiming with FOR UPDATE SKIP LOCKED so
multiple executor replicas can run side by side), executes the translated
PostgreSQL query under a read-only role and per-job statement timeout,
writes the serialized result to the shared results volume, and finalizes
the job phase. Also garbage-collects jobs past their destruction time.
"""

import datetime
import os
import shutil
import threading
import time

from prometheus_client import start_http_server
from ska_src_logging import LogContext
from tapcore import uws
from tapcore.config import settings
from tapcore.db import connection as db_connection
from tapcore.observability import (
    JOBS_BY_PHASE,
    JOBS_COMPLETED,
    OLDEST_QUEUED_JOB,
    QUERY_DURATION,
    REGISTRY,
    configure_logging,
    request_context,
    tag_sql,
)
from tapcore.query.adql import apply_maxrec, touched_tables
from tapcore.query.results import RowLimiter, columns_from_cursor, stream, tap_schema_metadata
from tapcore.query.upload import (
    create_upload_tables,
    load_uploads,
    parse_upload_param,
    rewrite_upload_refs,
)
from tapcore.query.votable import normalize_format

log = configure_logging("tap-executor")

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


class _AbortWatchdog:
    """Cancels this job's executing backend when the job turns ABORTED (or
    is deleted), regardless of when the abort arrives.

    The API's pg_cancel_backend() on ABORT is immediate but can miss: the
    abort may land before the backend PID is published, or between two
    statements (a cancel on an idle backend is a silent no-op). This
    watchdog closes those windows from the executor's side: it re-checks
    the job phase twice a second for the whole execution and keeps
    cancelling the backend until the statement actually stops. Some backend
    states honour cancellation only sporadically (e.g. deep in the
    planner), so after a few ignored cancels it escalates to
    pg_terminate_backend — safe here because the execution is read-only
    and the executor already handles a dead connection as an abort.
    """

    POLL_S = 0.5
    TERMINATE_AFTER_CANCELS = 6  # ~3s of ignored cancels

    def __init__(self, job_id: str, backend_pid: int):
        self._job_id = job_id
        self._pid = backend_pid
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        cancels = 0
        marker = uws.job_query_marker(self._job_id)
        while not self._stop.wait(self.POLL_S):
            try:
                with db_connection() as conn:
                    row = conn.execute(
                        "SELECT phase FROM uws.jobs WHERE job_id = %s", (self._job_id,)
                    ).fetchone()
                    if row is not None and row[0] != "ABORTED":
                        continue
                    # aborted (or deleted): interrupt the running statement,
                    # and keep doing so until execution ends. The signal is
                    # sent only while the backend still runs this job's
                    # cursor, so a reused PID is never hit.
                    if cancels < self.TERMINATE_AFTER_CANCELS:
                        conn.execute(
                            "SELECT pg_cancel_backend(pid) FROM pg_stat_activity"
                            " WHERE pid = %s AND query LIKE %s",
                            (self._pid, marker),
                        )
                        cancels += 1
                    else:
                        log.warning(
                            "job %s backend %d ignored %d cancels, terminating it",
                            self._job_id,
                            self._pid,
                            cancels,
                        )
                        conn.execute(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                            " WHERE pid = %s AND query LIKE %s",
                            (self._pid, marker),
                        )
            except Exception:  # never let monitoring kill the execution path
                log.exception("abort watchdog check failed for job %s", self._job_id)


def _reap_backend(job_id: str, pid: int | None) -> None:
    """After an abort or error, make sure this job's executing backend is
    really gone. Once the executor abandons its connection nothing else
    stops the server side: PostgreSQL only notices a lost client on the
    next send, so an orphaned backend can keep scanning for minutes. All
    checks and signals are scoped to the backend still running this job's
    cursor, so a reused PID is never touched."""
    if not pid:
        return
    marker = uws.job_query_marker(job_id)
    try:
        with db_connection() as conn:
            for attempt in range(8):
                row = conn.execute(
                    "SELECT state FROM pg_stat_activity WHERE pid = %s AND query LIKE %s",
                    (pid, marker),
                ).fetchone()
                if row is None or row[0] != "active":
                    return
                if attempt < 4:
                    conn.execute(
                        "SELECT pg_cancel_backend(pid) FROM pg_stat_activity"
                        " WHERE pid = %s AND query LIKE %s",
                        (pid, marker),
                    )
                else:
                    log.warning("backend %d survived cancels; terminating", pid)
                    conn.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                        " WHERE pid = %s AND query LIKE %s",
                        (pid, marker),
                    )
                time.sleep(0.5)
    except Exception:
        log.exception("failed to reap backend %s", pid)


def claim_job() -> dict | None:
    with db_connection() as conn:
        row = conn.execute(CLAIM_SQL).fetchone()
    if row is None:
        return None
    return uws._row_to_job(row)


def _count_finalized_elsewhere(job_id: str) -> None:
    """Count a job this executor worked on but did not finalize itself.

    Reaching one of these paths means the phase was no longer EXECUTING: an
    ABORT committed while we ran, or the row was deleted under us. The work
    happened either way, so leaving it out would make aborts invisible in the
    one metric that reports outcomes. The phase is read back rather than
    assumed, so the count says what the job actually became; a deleted row has
    no outcome left to report, and is only logged.
    """
    with db_connection() as conn:
        row = conn.execute("SELECT phase FROM uws.jobs WHERE job_id = %s", (job_id,)).fetchone()
    if row is None:
        log.info("job %s was deleted while running; no outcome to record", job_id)
        return
    phase = row[0]
    if phase == "EXECUTING":
        # nothing has finalized it after all, so it is not an outcome yet
        log.warning("job %s is still EXECUTING after failing to finalize", job_id)
        return
    JOBS_COMPLETED.labels(phase=phase).inc()


class _AsyncDuration:
    """Records an async job's query duration once, however the job ends.

    A flag and one ``finally`` rather than an observe at each exit: this
    function returns early at four points — an ABORT before execution, an
    ABORT during it, an error, and a job finalized under us — and a review
    found one of them recording nothing at all. With a single recording site,
    a path added later cannot quietly miss it.

    ``query_ran`` is what keeps the histogram meaning what it says: an ABORT
    that lands before the cursor executes spent no time querying, and counting
    its setup as a query duration would pull the low buckets down.
    """

    def __init__(self, started: float) -> None:
        self._started = started
        self.query_ran = False

    def __enter__(self) -> _AsyncDuration:
        return self

    def __exit__(self, *exc_info) -> bool:
        if self.query_ran:
            QUERY_DURATION.labels(kind="async").observe(time.monotonic() - self._started)
        return False


def execute_job(job: dict) -> None:
    job_id = job["job_id"]
    params = job["parameters"] or {}
    backend_pid = None
    started = time.monotonic()
    # scoped to the job: this loop runs for the life of the pod, so a leftover
    # id would attribute the next poll, the cleanup pass and any loop error to
    # whichever job happened to run last
    with (
        request_context(job.get("request_id")),
        LogContext(
            job_id=job_id,
            owner_id=job.get("owner_id"),
            request_id=job.get("request_id"),
        ),
        _AsyncDuration(started) as duration,
    ):
        _execute_job_inner(job, job_id, params, backend_pid, duration)


def _execute_job_inner(job: dict, job_id, params, backend_pid, duration) -> None:
    log.info("executing job %s", job_id)
    try:
        maxrec = min(int(params.get("MAXREC", settings.default_maxrec)), settings.hard_maxrec)
        fmt_key, mime, ext = normalize_format(params.get("RESPONSEFORMAT") or params.get("FORMAT"))
        sql = tag_sql(apply_maxrec(job["query_sql"], maxrec))

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
        with db_connection() as conn, conn.transaction():
            tap_meta = tap_schema_metadata(conn, touched_tables(job["query_sql"]))
            if uploads:
                create_upload_tables(conn, uploads, settings.query_role)
            timeout_ms = int(job["execution_duration"]) * 1000
            if timeout_ms > 0:
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(timeout_ms),),
                )
            # publish this backend's PID so ABORT can pg_cancel_backend() it —
            # conditionally, so an ABORT that already landed is honoured
            # before the query starts and never gains a stale PID
            backend_pid = pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            with db_connection() as side:
                published = side.execute(
                    "UPDATE uws.jobs SET backend_pid = %s"
                    " WHERE job_id = %s AND phase = 'EXECUTING'",
                    (pid, job_id),
                ).rowcount
                if not published:
                    shutil.rmtree(result_dir, ignore_errors=True)
                    _count_finalized_elsewhere(job_id)
                    log.info("job %s aborted before execution", job_id)
                    return
            # JIT compilation runs uninterruptibly at cursor DECLARE and can
            # ignore cancellation for many seconds on high-cost plans; it is
            # a net loss for streaming cursor workloads anyway
            conn.execute("SET LOCAL jit = off")
            conn.execute(f"SET LOCAL ROLE {settings.query_role}")
            with (
                _AbortWatchdog(job_id, pid),
                conn.cursor(name=f"tap_job_{job_id}") as cur,
            ):
                cur.itersize = 5000
                cur.execute(sql)
                duration.query_ran = True
                columns = columns_from_cursor(cur.description, tap_meta)
                limiter = RowLimiter(cur, maxrec)
                with open(result_path, "wb") as fh:
                    for chunk in stream(columns, limiter, fmt_key):
                        result_size += fh.write(chunk)

        with db_connection() as conn:
            # atomic transition: an ABORT committed at any point (even
            # between this statement and the stream ending) can never be
            # overwritten with COMPLETED
            completed = conn.execute(
                "UPDATE uws.jobs SET phase = 'COMPLETED', end_time = %s,"
                " result_mime = %s, result_size = %s, backend_pid = NULL"
                " WHERE job_id = %s AND phase = 'EXECUTING'",
                (_now(), mime, result_size, job_id),
            ).rowcount
        if not completed:  # aborted (or deleted) while running
            shutil.rmtree(result_dir, ignore_errors=True)
            _count_finalized_elsewhere(job_id)
            log.info("job %s finished but was already finalized; discarding", job_id)
            return
        JOBS_COMPLETED.labels(phase="COMPLETED").inc()
        log.info("job %s completed (%d rows, %s)", job_id, limiter.count, limiter.status)
    except Exception as exc:
        shutil.rmtree(uws.job_results_dir(job_id), ignore_errors=True)
        _reap_backend(job_id, backend_pid)
        with db_connection() as conn:
            current = uws.get_job(conn, job_id)
            if current["phase"] != "ABORTED":
                # grace re-read: an ABORT's phase commit can trail the
                # cancellation signal by a moment
                time.sleep(0.5)
                current = uws.get_job(conn, job_id)
            if current["phase"] == "ABORTED":
                # ABORT cancelled our statement mid-run: not an error
                JOBS_COMPLETED.labels(phase="ABORTED").inc()
                log.info("job %s aborted while executing", job_id)
                return
            # atomic: a racing ABORT wins, never overwritten with ERROR
            errored = conn.execute(
                "UPDATE uws.jobs SET phase = 'ERROR', end_time = %s,"
                " error_type = 'fatal', error_message = %s, backend_pid = NULL"
                " WHERE job_id = %s AND phase = 'EXECUTING'",
                (_now(), str(exc)[:4000], job_id),
            ).rowcount
            if errored:
                JOBS_COMPLETED.labels(phase="ERROR").inc()
                log.exception("job %s failed", job_id)
            else:
                # a racing ABORT (or a delete) won: whatever it became is the
                # outcome, and it is still an outcome
                _count_finalized_elsewhere(job_id)
                log.info("job %s aborted while executing", job_id)


def cleanup_expired() -> None:
    with db_connection() as conn:
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
            with db_connection() as conn:
                conn.execute("ALTER TABLE uws.jobs ADD COLUMN IF NOT EXISTS backend_pid integer")
                conn.execute("ALTER TABLE uws.jobs ADD COLUMN IF NOT EXISTS request_id text")
            return
        except Exception as exc:
            if attempt == attempts:
                raise
            log.warning("uws.jobs migration attempt %d failed (%s), retrying", attempt, exc)
            time.sleep(delay_s)


QUEUE_METRICS_INTERVAL_S = 5.0


def refresh_queue_metrics() -> None:
    """Publish the queue's depth and backlog.

    Reported by the executor because the queue is its subject, and because a
    deployment with no executor running has no queue to speak of. Every
    replica reports the same figures, so aggregate with max() rather than
    sum() — the numbers describe one shared queue, not each worker's share.
    """
    with db_connection() as conn:
        counts = conn.execute("SELECT phase, count(*) FROM uws.jobs GROUP BY phase").fetchall()
        oldest = conn.execute(
            "SELECT extract(epoch FROM now() - min(creation_time))"
            " FROM uws.jobs WHERE phase = 'QUEUED'"
        ).fetchone()
    # clear first: a phase that no longer has jobs must stop reporting its
    # last value rather than look stuck
    JOBS_BY_PHASE.clear()
    for phase, count in counts:
        JOBS_BY_PHASE.labels(phase=phase).set(count)
    OLDEST_QUEUED_JOB.set(float((oldest and oldest[0]) or 0.0))


def main() -> None:
    # A worker loop has no listener of its own, so metrics get one. Without it
    # the queue is invisible: nothing outside the database could say how deep
    # it is or how long jobs have waited.
    start_http_server(settings.executor_metrics_port, registry=REGISTRY)
    log.info(
        "tap-executor started (results dir: %s, metrics on :%d)",
        settings.results_dir,
        settings.executor_metrics_port,
    )
    os.makedirs(settings.results_dir, exist_ok=True)
    _ensure_backend_pid_column()
    last_cleanup = 0.0
    last_queue_metrics = 0.0
    while True:
        # survive transient failures (e.g. an ABORT's pg_cancel_backend
        # racing a finished execution and cancelling a pooled connection)
        try:
            job = claim_job()
            if job is not None:
                execute_job(job)
                continue
            if time.monotonic() - last_queue_metrics > QUEUE_METRICS_INTERVAL_S:
                refresh_queue_metrics()
                last_queue_metrics = time.monotonic()
            if time.monotonic() - last_cleanup > CLEANUP_INTERVAL_S:
                cleanup_expired()
                last_cleanup = time.monotonic()
        except Exception:
            log.exception("executor loop error, retrying")
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
