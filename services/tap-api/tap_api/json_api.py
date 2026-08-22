"""Modern JSON interface for machine-to-machine use, mounted at /api/v1.

This is a facade over the same engine as the IVOA TAP endpoints: the same
ADQL translation, TAP_SCHEMA publication checks, MAXREC limits, and UWS job
store — with JSON requests/responses (OpenAPI-documented via FastAPI)
instead of the XML the VO standards mandate. Standard VO clients keep using
/tap; services and pipelines can use this API.

It also accepts SKA SRC ingestion notifications, validated with the
ska-src-mm-notification pydantic models, and stores them in the srcnet
tables generated from those same models (see tap_api.odp) — so ingested
observatory data product metadata is instantly queryable via both this
API and TAP/ADQL.
"""

import asyncio
import datetime
import os
import shutil
import time

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from ska_src_mm_notification.models.schemas.srcnet_ingestion import SRCIngestionNotification
from tapcore import uws
from tapcore.config import settings
from tapcore.db import pool
from tapcore.errors import NotFoundError, UsageError

from .odp import TABLES, amend_rows, fetch_notification, ingest_notification
from .query import prepare_query, run_sync

router = APIRouter(prefix="/api/v1", tags=["json-api"])


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    query: str = Field(description="ADQL query", min_length=1)
    lang: str = Field(default="ADQL", description="Query language (ADQL or ADQL-2.0)")
    maxrec: int | None = Field(default=None, ge=0, description="Row limit (MAXREC)")
    format: str = Field(
        default="json",
        description="Result format: json (default), votable, csv, tsv, parquet, or arrow",
    )


class JobRequest(QueryRequest):
    format: str = Field(
        default="votable",
        description="Result format: votable, csv, tsv, json, parquet, or arrow",
    )
    run: bool = Field(default=False, description="Queue the job for execution immediately")
    run_id: str | None = Field(default=None, description="Client-supplied run identifier")


def _tap_params(body: QueryRequest, fmt: str | None = None) -> dict[str, str]:
    params = {"LANG": body.lang, "QUERY": body.query}
    if body.maxrec is not None:
        params["MAXREC"] = str(body.maxrec)
    if fmt is not None:
        params["RESPONSEFORMAT"] = fmt
    return params


@router.post("/query")
async def sync_query(body: QueryRequest):
    """Synchronous ADQL query, JSON by default (metadata, data, status).

    Set ``format`` to ``parquet`` or ``arrow`` for columnar responses.
    """
    prepared = prepare_query(_tap_params(body, fmt=body.format))
    chunks, mime = run_sync(prepared)
    return StreamingResponse(chunks, media_type=mime)


# ---------------------------------------------------------------------------
# Jobs (JSON facade over the shared UWS job store)
# ---------------------------------------------------------------------------


def _iso(dt: datetime.datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _api_base() -> str:
    """The /api/v1 base derived from the TAP base URL (…/tap -> …/api/v1)."""
    root = settings.base_url.rsplit("/tap", 1)[0]
    return f"{root}/api/v1"


def _job_json(job: dict) -> dict:
    body = {
        "job_id": job["job_id"],
        "phase": job["phase"],
        "run_id": job["run_id"],
        "owner_id": job["owner_id"],
        "creation_time": _iso(job["creation_time"]),
        "start_time": _iso(job["start_time"]),
        "end_time": _iso(job["end_time"]),
        "execution_duration": job["execution_duration"],
        "destruction": _iso(job["destruction"]),
        "parameters": job["parameters"],
        "urls": {
            "job": f"{_api_base()}/jobs/{job['job_id']}",
            "uws": f"{settings.base_url}/async/{job['job_id']}",
        },
    }
    if job["phase"] == "COMPLETED":
        body["result"] = {
            "href": f"{_api_base()}/jobs/{job['job_id']}/result",
            "mime": job["result_mime"],
            "size": job["result_size"],
        }
    if job["phase"] == "ERROR":
        body["error"] = {"type": job["error_type"], "message": job["error_message"]}
    return body


def _queue(conn, job: dict) -> None:
    if job["phase"] not in ("PENDING", "HELD"):
        raise UsageError(f"cannot start job in phase {job['phase']}")
    prepared = prepare_query(job["parameters"])
    uws.update_job(conn, job["job_id"], phase="QUEUED", query_sql=prepared["sql"])


@router.post("/jobs", status_code=201)
async def create_job(body: JobRequest):
    params = _tap_params(body, fmt=body.format)
    if body.run_id:
        params["RUNID"] = body.run_id
    prepare_query(params)  # validate before storing, unlike lenient UWS XML flow
    with pool().connection() as conn:
        job = uws.create_job(conn, params)
        if body.run:
            _queue(conn, job)
        return _job_json(uws.get_job(conn, job["job_id"]))


@router.get("/jobs")
async def list_jobs(phase: str | None = None, last: int | None = None, after: str | None = None):
    if last is not None and last < 1:
        raise UsageError("last must be a positive integer")
    phases = [p.strip().upper() for p in phase.split(",")] if phase else None
    for item in phases or []:
        if item not in uws.ALL_PHASES:
            raise UsageError(f"unknown phase {item}")
    since = None
    if after is not None:
        try:
            since = datetime.datetime.fromisoformat(after.replace("Z", "+00:00"))
        except ValueError:
            raise UsageError("after must be an ISO-8601 timestamp") from None
    with pool().connection() as conn:
        jobs = uws.list_jobs(conn, phases, last, since)
    return {"jobs": [_job_json(j) for j in jobs]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, wait: int | None = None):
    """Job status; ``wait`` blocks (UWS 1.1 semantics, capped at the server
    maximum) until the phase changes or the time expires."""
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    if wait and job["phase"] in uws.ACTIVE_PHASES:
        from .uws_api import WAIT_POLL_S

        reference = job["phase"]
        deadline = time.monotonic() + min(wait, settings.wait_max_s)
        while job["phase"] == reference and time.monotonic() < deadline:
            await asyncio.sleep(min(WAIT_POLL_S, max(0.0, deadline - time.monotonic())))
            with pool().connection() as conn:
                job = uws.get_job(conn, job_id)
    return _job_json(job)


class PhaseRequest(BaseModel):
    phase: str = Field(description="RUN or ABORT")


@router.post("/jobs/{job_id}/phase")
async def post_phase(job_id: str, body: PhaseRequest):
    phase = body.phase.upper()
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
        if phase == "RUN":
            _queue(conn, job)
        elif phase == "ABORT":
            uws.abort_job(conn, job)
        else:
            raise UsageError("phase must be RUN or ABORT")
        return _job_json(uws.get_job(conn, job_id))


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    with pool().connection() as conn:
        uws.delete_job(conn, job_id)
    shutil.rmtree(uws.job_results_dir(job_id), ignore_errors=True)
    return Response(status_code=204)


@router.get("/jobs/{job_id}/result")
async def get_result(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    if job["phase"] != "COMPLETED":
        raise NotFoundError(f"job {job_id} has no result (phase {job['phase']})")
    result_dir = uws.job_results_dir(job_id)
    for name in os.listdir(result_dir) if os.path.isdir(result_dir) else []:
        if name.startswith("result."):
            return FileResponse(
                os.path.join(result_dir, name),
                media_type=job["result_mime"] or "application/octet-stream",
            )
    raise NotFoundError(f"result file for job {job_id} not found")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@router.get("/tables")
async def tables():
    """JSON rendering of TAP_SCHEMA (machine-friendly alternative to VOSI XML)."""
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT t.schema_name, t.table_name, t.description,
                   jsonb_agg(jsonb_build_object(
                       'name', c.column_name, 'datatype', c.datatype,
                       'unit', c.unit, 'ucd', c.ucd, 'description', c.description)
                       ORDER BY c.column_index)
            FROM tap_schema.tables t
            JOIN tap_schema.columns c ON c.table_name = t.table_name
            GROUP BY t.schema_name, t.table_name, t.description, t.table_index
            ORDER BY t.schema_name, t.table_index
            """
        ).fetchall()
    return {
        "tables": [
            {"schema": r[0], "name": r[1], "description": r[2], "columns": r[3]} for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# SRC ingestion notifications
# ---------------------------------------------------------------------------


@router.post("/notifications", status_code=201)
async def ingest(notification: SRCIngestionNotification):
    """Validate (ska-src-mm-notification models) and store a notification.

    The payload is flattened into the srcnet.* tables generated from the
    same models; re-posting a notification upserts (idempotent).
    """
    with pool().connection() as conn:
        counts = ingest_notification(conn, notification)
    return {
        "status": "ingested",
        "project_id": notification.project_id,
        "schema_version": notification.schema_version,
        "rows": counts,
        "query_hint": (
            "SELECT * FROM srcnet.data_products WHERE project_id = "
            f"'{notification.project_id}' (via /tap/sync or /api/v1/query)"
        ),
    }


@router.get("/notifications")
async def list_notifications():
    tables = {t.name: t.qualified for t in TABLES}
    with pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT p.project_id, p.project_title, p.data_rights,
                   (SELECT count(*) FROM {tables["data_products"]} d
                     WHERE d.project_id = p.project_id),
                   (SELECT count(*) FROM {tables["artifacts"]} a
                     WHERE a.project_id = p.project_id)
            FROM {tables["projects"]} p
            ORDER BY p.project_id
            """
        ).fetchall()
    return {
        "projects": [
            {
                "project_id": r[0],
                "project_title": r[1],
                "data_rights": r[2],
                "data_products": r[3],
                "artifacts": r[4],
            }
            for r in rows
        ]
    }


@router.get("/notifications/{project_id}")
async def get_notification(project_id: str):
    with pool().connection() as conn:
        document = fetch_notification(conn, project_id)
    if document is None:
        raise NotFoundError(f"project {project_id} not found")
    return document


class AmendRequest(BaseModel):
    table: str = Field(
        description="Target table: projects, observations, scheduling_blocks,"
        " execution_blocks, data_products, or artifacts"
    )
    match: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Column equality filters selecting the rows to amend"
        " (empty = every row of the project in that table)",
    )
    values: dict = Field(
        description="Columns to set; each value is validated against the"
        " corresponding pydantic model field"
    )


@router.patch("/notifications/{project_id}")
async def amend_notification(project_id: str, body: AmendRequest):
    """Amend already-ingested rows — e.g. backfill a column added by a
    newer data-model release — without re-sending the whole notification.

    The update is validated field-by-field with the ska-src-mm-notification
    models and always scoped to the given project.
    """
    with pool().connection() as conn, conn.transaction():
        if fetch_notification(conn, project_id) is None:
            raise NotFoundError(f"project {project_id} not found")
        updated = amend_rows(conn, project_id, body.table, dict(body.match), dict(body.values))
    return {
        "status": "amended",
        "project_id": project_id,
        "table": body.table,
        "updated": updated,
    }
