"""UWS 1.1 job model: persistence in uws.jobs and XML rendering."""

import datetime
import json
import os
import re
import secrets
import time
from xml.etree import ElementTree as ET

from .auth.context import current_job_viewer
from .config import settings
from .errors import AuthorizationError, NotFoundError
from .observability import request_id

JOB_ID_RE = re.compile(r"^[0-9a-f]{16}$")

UWS_NS = "http://www.ivoa.net/xml/UWS/v1.0"
XLINK_NS = "http://www.w3.org/1999/xlink"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

ACTIVE_PHASES = {"PENDING", "QUEUED", "EXECUTING", "HELD", "SUSPENDED"}
FINAL_PHASES = {"COMPLETED", "ERROR", "ABORTED", "ARCHIVED"}
ALL_PHASES = ACTIVE_PHASES | FINAL_PHASES

JOB_COLUMNS = (
    "job_id, phase, run_id, owner_id, quote, creation_time, start_time, end_time, "
    "execution_duration, destruction, parameters, query_sql, query_tables, error_type, "
    "error_message, result_mime, result_size, backend_pid, request_id"
)


def _row_to_job(row) -> dict:
    keys = [c.strip() for c in JOB_COLUMNS.split(",")]
    return dict(zip(keys, row, strict=True))


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
        f"SELECT {JOB_COLUMNS} FROM uws.jobs WHERE job_id = %s", (job_id,)
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
    sql = f"SELECT {JOB_COLUMNS} FROM uws.jobs"
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
    sql += " ORDER BY creation_time DESC"
    if last:
        sql += " LIMIT %s"
        args.append(last)
    return [_row_to_job(r) for r in conn.execute(sql, args).fetchall()]


def update_job(conn, job_id: str, **fields) -> None:
    if not fields:
        return
    _check_owner_of(conn, job_id)
    sets = ", ".join(f"{k} = %s" for k in fields)
    values = [json.dumps(v) if k == "parameters" else v for k, v in fields.items()]
    cur = conn.execute(f"UPDATE uws.jobs SET {sets} WHERE job_id = %s", (*values, job_id))
    if cur.rowcount == 0:
        raise NotFoundError(f"job {job_id} not found")


CANCEL_RETRIES = 5
CANCEL_RETRY_DELAY_S = 0.2


def job_query_tag(job_id: str) -> str:
    """Comment prefix naming the job in ``pg_stat_activity.query``.

    A prefix, not a suffix like ``tag_sql``'s request id, because the
    activity view truncates long statements (track_activity_query_size)
    and the abort path must find this marker on exactly the queries big
    enough to be worth aborting. It used to be the DECLARE'd cursor's name
    that carried the job id here; the tag replaces it now that result
    queries run as plain streamed statements."""
    return f"/* tap_job_{job_id} */ "


def job_query_marker(job_id: str) -> str:
    """SQL LIKE pattern identifying this job's executing statement (tagged
    with ``job_query_tag``), used to validate a backend's identity before
    signalling it — a stale or reused PID never gets cancelled."""
    return f"%tap_job_{job_id}%"


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
        conn.execute(
            "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE pid = %s AND query LIKE %s",
            (pid, marker),
        )
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


# ---------------------------------------------------------------------------
# XML rendering
# ---------------------------------------------------------------------------


def _el(parent, tag, text=None, nil=False, **attrs):
    element = ET.SubElement(parent, f"{{{UWS_NS}}}{tag}", **attrs)
    if nil:
        element.set(f"{{{XSI_NS}}}nil", "true")
    elif text is not None:
        element.text = str(text)
    return element


def result_url(job_id: str) -> str:
    return f"{settings.base_url}/async/{job_id}/results/result"


def job_xml(job: dict) -> bytes:
    def _nillable(tag: str, value) -> None:
        if value is None:
            _el(root, tag, nil=True)
        else:
            _el(root, tag, value)

    root = ET.Element(f"{{{UWS_NS}}}job", {"version": "1.1"})
    _el(root, "jobId", job["job_id"])
    _nillable("runId", job["run_id"])
    _nillable("ownerId", job["owner_id"])
    _el(root, "phase", job["phase"])
    _nillable("quote", iso_utc(job["quote"]))
    _el(root, "creationTime", iso_utc(job["creation_time"]))
    _nillable("startTime", iso_utc(job["start_time"]))
    _nillable("endTime", iso_utc(job["end_time"]))
    _el(root, "executionDuration", job["execution_duration"])
    _el(root, "destruction", iso_utc(job["destruction"]))

    params = _el(root, "parameters")
    for key, value in (job["parameters"] or {}).items():
        param = _el(params, "parameter", value)
        param.set("id", key.lower())

    results = _el(root, "results")
    if job["phase"] == "COMPLETED":
        result = _el(results, "result")
        result.set("id", "result")
        result.set(f"{{{XLINK_NS}}}href", result_url(job["job_id"]))
        if job["result_mime"]:
            result.set("mime-type", job["result_mime"])

    if job["phase"] == "ERROR" and job["error_message"]:
        err = _el(root, "errorSummary")
        err.set("type", job["error_type"] or "fatal")
        err.set("hasDetail", "true")
        _el(err, "message", job["error_message"])

    return _serialize(root)


def joblist_xml(jobs: list[dict]) -> bytes:
    root = ET.Element(f"{{{UWS_NS}}}jobs", {"version": "1.1"})
    for job in jobs:
        ref = _el(root, "jobref")
        ref.set("id", job["job_id"])
        ref.set(f"{{{XLINK_NS}}}href", f"{settings.base_url}/async/{job['job_id']}")
        _el(ref, "phase", job["phase"])
    return _serialize(root)


def _serialize(root) -> bytes:
    ET.register_namespace("uws", UWS_NS)
    ET.register_namespace("xlink", XLINK_NS)
    ET.register_namespace("xsi", XSI_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
