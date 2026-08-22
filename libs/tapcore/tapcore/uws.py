"""UWS 1.1 job model: persistence in uws.jobs and XML rendering."""

import datetime
import json
import secrets
from xml.etree import ElementTree as ET

from .config import settings
from .errors import NotFoundError

UWS_NS = "http://www.ivoa.net/xml/UWS/v1.0"
XLINK_NS = "http://www.w3.org/1999/xlink"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

ACTIVE_PHASES = {"PENDING", "QUEUED", "EXECUTING", "HELD", "SUSPENDED"}
FINAL_PHASES = {"COMPLETED", "ERROR", "ABORTED", "ARCHIVED"}
ALL_PHASES = ACTIVE_PHASES | FINAL_PHASES

JOB_COLUMNS = (
    "job_id, phase, run_id, owner_id, quote, creation_time, start_time, end_time, "
    "execution_duration, destruction, parameters, query_sql, error_type, "
    "error_message, result_mime, result_size"
)


def _row_to_job(row) -> dict:
    keys = [c.strip() for c in JOB_COLUMNS.split(",")]
    return dict(zip(keys, row, strict=True))


def new_job_id() -> str:
    return secrets.token_hex(8)


def _iso(dt: datetime.datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_job(conn, parameters: dict[str, str], owner_id: str | None = None) -> dict:
    job_id = new_job_id()
    now = datetime.datetime.now(datetime.UTC)
    destruction = now + datetime.timedelta(seconds=settings.job_retention_s)
    conn.execute(
        """
        INSERT INTO uws.jobs (job_id, phase, run_id, owner_id, creation_time,
                              execution_duration, destruction, parameters)
        VALUES (%s, 'PENDING', %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            job_id,
            parameters.get("RUNID"),
            owner_id,
            now,
            settings.default_exec_duration_s,
            destruction,
            json.dumps(parameters),
        ),
    )
    return get_job(conn, job_id)


def get_job(conn, job_id: str) -> dict:
    row = conn.execute(
        f"SELECT {JOB_COLUMNS} FROM uws.jobs WHERE job_id = %s", (job_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"job {job_id} not found")
    return _row_to_job(row)


def list_jobs(conn, phases: list[str] | None = None, last: int | None = None) -> list[dict]:
    sql = f"SELECT {JOB_COLUMNS} FROM uws.jobs"
    args: list = []
    if phases:
        sql += " WHERE phase = ANY(%s)"
        args.append(phases)
    else:
        sql += " WHERE phase <> 'ARCHIVED'"
    sql += " ORDER BY creation_time DESC"
    if last:
        sql += " LIMIT %s"
        args.append(last)
    return [_row_to_job(r) for r in conn.execute(sql, args).fetchall()]


def update_job(conn, job_id: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = %s" for k in fields)
    values = [json.dumps(v) if k == "parameters" else v for k, v in fields.items()]
    cur = conn.execute(f"UPDATE uws.jobs SET {sets} WHERE job_id = %s", (*values, job_id))
    if cur.rowcount == 0:
        raise NotFoundError(f"job {job_id} not found")


def delete_job(conn, job_id: str) -> None:
    cur = conn.execute("DELETE FROM uws.jobs WHERE job_id = %s", (job_id,))
    if cur.rowcount == 0:
        raise NotFoundError(f"job {job_id} not found")


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
    _nillable("quote", _iso(job["quote"]))
    _el(root, "creationTime", _iso(job["creation_time"]))
    _nillable("startTime", _iso(job["start_time"]))
    _nillable("endTime", _iso(job["end_time"]))
    _el(root, "executionDuration", job["execution_duration"])
    _el(root, "destruction", _iso(job["destruction"]))

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
