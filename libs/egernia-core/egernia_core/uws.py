"""UWS 1.1 job model: persistence in uws.jobs, and job cancellation.

XML rendering lives in :mod:`egernia_core.uws_xml`; only the API needs it.
"""

import datetime
import json
import os
import re
import secrets
import time
from collections.abc import Iterable

from .auth.context import current_job_viewer
from .config import settings
from .errors import AuthorizationError, NotFoundError
from .observability import request_id

JOB_ID_RE = re.compile(r"^[0-9a-f]{16}$")

ACTIVE_PHASES = {"PENDING", "QUEUED", "EXECUTING", "HELD", "SUSPENDED"}
FINAL_PHASES = {"COMPLETED", "ERROR", "ABORTED", "ARCHIVED"}
ALL_PHASES = ACTIVE_PHASES | FINAL_PHASES
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000

#: the job row, in SELECT order. A tuple rather than a comma-joined string
#: because it is read back per row: splitting and stripping nineteen names
#: again for every job was the row loop's largest avoidable cost.
JOB_COLUMNS = (
    "job_id",
    "phase",
    "run_id",
    "owner_id",
    "quote",
    "creation_time",
    "start_time",
    "end_time",
    "execution_duration",
    "destruction",
    "parameters",
    "query_sql",
    "query_tables",
    "error_type",
    "error_message",
    "result_mime",
    "result_size",
    "backend_pid",
    "request_id",
    "worker_id",
    "lease_expires",
)
JOB_COLUMNS_SQL = ", ".join(JOB_COLUMNS)


def _row_to_job(row) -> dict:
    return dict(zip(JOB_COLUMNS, row, strict=True))


def new_job_id() -> str:
    return secrets.token_hex(8)


def job_results_dir(job_id: str) -> str:
    """The job's directory under the results volume.

    Validates the id against the server-generated format, then normalizes
    the joined path and verifies it stays inside the results directory, so
    a crafted id can never traverse outside it.
    """
    if not JOB_ID_RE.fullmatch(job_id):
        raise NotFoundError(f"job {job_id} not found")
    base = os.path.abspath(settings.results_dir)
    path = os.path.normpath(os.path.join(base, job_id))
    if not path.startswith(base + os.sep):
        raise NotFoundError(f"job {job_id} not found")
    return path


def iso_utc(dt: datetime.datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_job_columns(conn) -> None:
    """Forward-migrate uws.jobs columns older deployments lack (fresh
    databases get them from db/init). Called by both services: in a rolling
    upgrade either one can be the first to touch the table."""
    conn.execute("ALTER TABLE uws.jobs ADD COLUMN IF NOT EXISTS backend_pid integer")
    conn.execute("ALTER TABLE uws.jobs ADD COLUMN IF NOT EXISTS request_id text")
    conn.execute("ALTER TABLE uws.jobs ADD COLUMN IF NOT EXISTS query_tables text[]")
    conn.execute("ALTER TABLE uws.jobs ADD COLUMN IF NOT EXISTS worker_id text")
    conn.execute("ALTER TABLE uws.jobs ADD COLUMN IF NOT EXISTS lease_expires timestamptz")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS jobs_expired_leases"
        " ON uws.jobs (lease_expires) WHERE phase = 'EXECUTING'"
    )


def create_job(conn, parameters: dict[str, str], owner_id: str | None = None) -> dict:
    job_id = new_job_id()
    now = datetime.datetime.now(datetime.UTC)
    destruction = now + datetime.timedelta(seconds=settings.job_retention_s)
    conn.execute(
        """
        INSERT INTO uws.jobs (job_id, phase, run_id, owner_id, creation_time,
                              execution_duration, destruction, parameters,
                              request_id)
        VALUES (%s, 'PENDING', %s, %s, %s, %s, %s, %s::jsonb, %s)
        """,
        (
            job_id,
            parameters.get("RUNID"),
            owner_id,
            now,
            settings.default_exec_duration_s,
            destruction,
            json.dumps(parameters),
            # the request that created the job, so the executor's records and
            # the API's describe the same piece of work
            request_id(),
        ),
    )
    return get_job(conn, job_id)


def get_job(conn, job_id: str) -> dict:
    row = conn.execute(
        f"SELECT {JOB_COLUMNS_SQL} FROM uws.jobs WHERE job_id = %s", (job_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"job {job_id} not found")
    job = _row_to_job(row)
    _check_ownership(job)
    return job


def _check_ownership(job: dict) -> None:
    """Refuse a job that belongs to somebody else.

    Enforced here rather than at each endpoint: every route that can reach a
    job — the UWS resources and their sub-resources, the JSON facade, the
    result download — goes through this function or one of the mutators
    below, so a newly added resource is covered by construction.
    """
    viewer = current_job_viewer()
    if viewer is None or viewer.may_see(job.get("owner_id")):
        return
    raise AuthorizationError(f"job {job['job_id']} belongs to another user")


def list_jobs(
    conn,
    phases: list[str] | None = None,
    last: int | None = None,
    after: datetime.datetime | None = None,
) -> list[dict]:
    sql = f"SELECT {JOB_COLUMNS_SQL} FROM uws.jobs"
    args: list = []
    if phases:
        sql += " WHERE phase = ANY(%s)"
        args.append(phases)
    else:
        sql += " WHERE phase <> 'ARCHIVED'"
    if after is not None:  # UWS 1.1 AFTER: jobs created later than the instant
        sql += " AND creation_time > %s"
        args.append(after)
    viewer = current_job_viewer()
    if viewer is not None:
        # own jobs, plus the ownerless ones anonymous callers create
        sql += " AND (owner_id IS NULL OR owner_id = %s)"
        args.append(viewer.subject)
    sql += " ORDER BY creation_time DESC LIMIT %s"
    args.append(min(last or DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT))
    return [_row_to_job(r) for r in conn.execute(sql, args).fetchall()]


def update_job(conn, job_id: str, expected_phases: Iterable[str] | None = None, **fields) -> bool:
    if not fields:
        return True
    _check_owner_of(conn, job_id)
    sets = ", ".join(f"{k} = %s" for k in fields)
    values = [json.dumps(v) if k == "parameters" else v for k, v in fields.items()]
    where = "job_id = %s"
    args = [*values, job_id]
    if expected_phases is not None:
        where += " AND phase = ANY(%s)"
        args.append(list(expected_phases))
    cur = conn.execute(f"UPDATE uws.jobs SET {sets} WHERE {where}", args)
    if cur.rowcount == 0 and expected_phases is None:
        raise NotFoundError(f"job {job_id} not found")
    if cur.rowcount == 0:
        _check_owner_of(conn, job_id)  # distinguish a missing job from a lost race
    return cur.rowcount == 1


CANCEL_RETRIES = 5
CANCEL_RETRY_DELAY_S = 0.2


def job_query_tag(job_id: str) -> str:
    """Comment prefix naming the job in ``pg_stat_activity.query``.

    A prefix, not a suffix like ``tag_sql``'s request id, because the
    activity view truncates long statements (track_activity_query_size)
    and the abort path must find this marker on exactly the queries big
    enough to be worth aborting."""
    return f"/* tap_job_{job_id} */ "


def job_query_marker(job_id: str) -> str:
    """SQL LIKE pattern identifying this job's executing statement (tagged
    with ``job_query_tag``), used to validate a backend's identity before
    signalling it — a stale or reused PID never gets cancelled."""
    return f"%tap_job_{job_id}%"


def signal_backend(conn, pid: int, marker: str, *, terminate: bool = False) -> None:
    """Cancel — or terminate — the backend running this job's statement.

    The ``query LIKE`` guard is the safety property, not decoration: the
    signal lands only while that backend is still running the tagged
    statement, so a PID PostgreSQL has since handed to somebody else is never
    hit. Written once because it was written five times, and a call site that
    forgot the guard would cancel an unrelated query.
    """
    action = "pg_terminate_backend" if terminate else "pg_cancel_backend"
    conn.execute(
        f"SELECT {action}(pid) FROM pg_stat_activity WHERE pid = %s AND query LIKE %s",
        (pid, marker),
    )


def abort_job(conn, job: dict) -> None:
    """Move an active job to ABORTED and cancel its running statement.

    The transition is a single conditional UPDATE, so a concurrent
    completion or error can never be overwritten (and the PID used is the
    one captured atomically by that transition, not a stale snapshot). The
    update is committed *before* pg_cancel_backend() fires, so the
    executor's cancellation handler always observes ABORTED. Cancels are
    validated against pg_stat_activity — the backend must still be running
    this job's cursor — so a finished or reused PID is never signalled; a
    cancel that lands between statements is a silent no-op, hence the
    retry while the statement stays active.
    """
    row = conn.execute(
        "UPDATE uws.jobs SET phase = 'ABORTED', end_time = %s"
        " WHERE job_id = %s AND phase = ANY(%s) RETURNING backend_pid",
        (datetime.datetime.now(datetime.UTC), job["job_id"], list(ACTIVE_PHASES)),
    ).fetchone()
    conn.commit()  # make ABORTED visible before the executor is interrupted
    if row is None or not row[0]:  # already final, or no execution to stop
        return
    pid = row[0]
    marker = job_query_marker(job["job_id"])
    for _ in range(CANCEL_RETRIES):
        signal_backend(conn, pid, marker)
        conn.commit()
        time.sleep(CANCEL_RETRY_DELAY_S)
        active = conn.execute(
            "SELECT 1 FROM pg_stat_activity WHERE pid = %s AND state = 'active' AND query LIKE %s",
            (pid, marker),
        ).fetchone()
        if active is None:
            return


def delete_job(conn, job_id: str) -> None:
    _check_owner_of(conn, job_id)
    cur = conn.execute("DELETE FROM uws.jobs WHERE job_id = %s", (job_id,))
    if cur.rowcount == 0:
        raise NotFoundError(f"job {job_id} not found")


def _check_owner_of(conn, job_id: str) -> None:
    """Ownership check for the mutators, which do not read the row first."""
    if current_job_viewer() is None:
        return
    row = conn.execute("SELECT owner_id FROM uws.jobs WHERE job_id = %s", (job_id,)).fetchone()
    if row is None:
        return  # the mutator reports the missing job itself
    _check_ownership({"job_id": job_id, "owner_id": row[0]})
