"""Modern JSON interface for machine-to-machine use, mounted at /api/v1.

This is a facade over the same engine as the IVOA TAP endpoints: the same
ADQL translation, TAP_SCHEMA publication checks, MAXREC limits, and UWS job
store — with JSON requests/responses (OpenAPI-documented via FastAPI)
instead of the XML the VO standards mandate. Standard VO clients keep using
/tap; services and pipelines can use this API.

It also publishes the active metadata-domain plugins (tapcore.metadata.plugins):
each plugin's documents are validated with its own pydantic models and
stored in tables generated from those same models — so ingested metadata
is instantly queryable via both this API and TAP/ADQL. The observatory
data product domain (tap_api.plugins.odp, srcnet schema) and the software
discovery domain (tap_api.plugins.software) ship built in; third-party model
packages register through the skao_tap.models entry-point group.
"""

import asyncio
import datetime
import logging
import os
import shutil
import time

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from tapcore import uws
from tapcore.config import settings
from tapcore.db import pool
from tapcore.errors import NotFoundError, UsageError
from tapcore.metadata import ingest
from tapcore.metadata.plugins import MetadataPlugin, active_plugins

from ..auth import auth_summary, gated, owner_of, require
from ..queries.query import prepare_query, run_sync

router = APIRouter(prefix="/api/v1", tags=["json-api"])
log = logging.getLogger("tap_api")


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


#: which requests each gated operation covers, for /api/v1/auth
OPERATION_ROUTES = {
    "metadata.ingest": "POST /api/v1/<mount>",
    "metadata.amend": "PATCH /api/v1/<mount>/{root_id}",
    "metadata.delete": "DELETE /api/v1/<mount>/{root_id}",
    "jobs.create": "POST /tap/async, POST /api/v1/jobs",
    "jobs.mutate": (
        "POST /tap/async/{job_id}/(phase|executionduration|destruction|parameters),"
        " POST /api/v1/jobs/{job_id}/phase"
    ),
    "jobs.delete": (
        "DELETE /tap/async/{job_id}, POST /tap/async/{job_id} with ACTION=DELETE,"
        " DELETE /api/v1/jobs/{job_id}"
    ),
    "query.sync": "GET|POST /tap/sync, POST /api/v1/query",
}


@router.get("/auth")
async def auth_info():
    """What this deployment enforces: whether a token is needed, and from where.

    Clients (and operators) should not have to discover by trial that a
    deployment is unauthenticated, or which IAM issues the tokens it accepts.
    """
    summary = auth_summary()
    # only what this deployment actually enforces: listing an operation it
    # lets through would tell a client to send a token it does not need — and
    # with authentication off it enforces nothing at all, whatever the gate
    # set happens to say
    enforced = gated() if summary["enabled"] else ()
    summary["gated_operations"] = {name: OPERATION_ROUTES[name] for name in enforced}
    return summary


@router.post("/query", dependencies=[Depends(require("query.sync"))])
async def sync_query(body: QueryRequest):
    """Synchronous ADQL query, JSON by default (metadata, data, status).

    Set ``format`` to ``parquet`` or ``arrow`` for columnar responses.
    """
    prepared = await run_in_threadpool(prepare_query, _tap_params(body, fmt=body.format))
    chunks, mime = run_sync(prepared)
    return StreamingResponse(chunks, media_type=mime)


# ---------------------------------------------------------------------------
# Jobs (JSON facade over the shared UWS job store)
# ---------------------------------------------------------------------------


def _iso(dt: datetime.datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # pyright: ignore


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


def _queue(conn, job: dict, prepared: dict) -> None:
    """Move a job to QUEUED, using an already-translated query.

    ``prepared`` is required rather than optional: translating here would put
    an ADQL parse back on the event loop, since every caller is an async
    handler. Required means a future caller cannot reintroduce that quietly.
    """
    if job["phase"] not in ("PENDING", "HELD"):
        raise UsageError(f"cannot start job in phase {job['phase']}")
    uws.update_job(conn, job["job_id"], phase="QUEUED", query_sql=prepared["sql"])


@router.post("/jobs", status_code=201, dependencies=[Depends(require("jobs.create"))])
async def create_job(body: JobRequest, request: Request):
    params = _tap_params(body, fmt=body.format)
    if body.run_id:
        params["RUNID"] = body.run_id
    # validate before storing, unlike the lenient UWS XML flow — and keep the
    # result, so running the job now does not re-translate the same query
    prepared = await run_in_threadpool(prepare_query, params)
    with pool().connection() as conn:
        job = uws.create_job(conn, params, owner_id=owner_of(request))
        if body.run:
            _queue(conn, job, prepared)
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
    maximum; ``-1`` means the maximum) until the phase changes or the time
    expires."""
    if wait is not None and wait < -1:
        raise UsageError("wait must be >= -1")
    if wait == -1:
        wait = settings.wait_max_s
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


@router.post("/jobs/{job_id}/phase", dependencies=[Depends(require("jobs.mutate"))])
async def post_phase(job_id: str, body: PhaseRequest):
    phase = body.phase.upper()
    prepared = None
    if phase == "RUN":
        # the job's own parameters, translated off the event loop
        with pool().connection() as conn:
            stored = uws.get_job(conn, job_id)
        prepared = await run_in_threadpool(prepare_query, stored["parameters"])
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
        if prepared is not None:  # set exactly when the phase is RUN
            _queue(conn, job, prepared)
        elif phase == "ABORT":
            uws.abort_job(conn, job)
        else:
            raise UsageError("phase must be RUN or ABORT")
        return _job_json(uws.get_job(conn, job_id))


@router.delete("/jobs/{job_id}", status_code=204, dependencies=[Depends(require("jobs.delete"))])
async def delete_job(job_id: str):
    with pool().connection() as conn:
        uws.delete_job(conn, job_id)
    try:
        shutil.rmtree(uws.job_results_dir(job_id))
    except FileNotFoundError:
        pass  # nothing was ever written for this job
    except OSError:
        # the job row is gone either way. The id is not interpolated into the
        # message — it reaches the log as a path-derived request value, which
        # CodeQL flags as log injection (py/log-injection) whatever format
        # check it passed — but the traceback names the directory that could
        # not be removed, so the record stays actionable.
        log.warning("failed to remove the result files of a deleted job", exc_info=True)
    return Response(status_code=204)


@router.get("/jobs/{job_id}/result")
async def get_result(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    if job["phase"] != "COMPLETED":
        raise NotFoundError(f"job {job_id} has no result (phase {job['phase']})")
    result_dir = uws.job_results_dir(job_id)
    try:
        result_names = os.listdir(result_dir) if os.path.isdir(result_dir) else []
    except OSError:
        result_names = []
    for name in result_names:
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
# Metadata-domain plugins: one ingest/list/fetch/amend endpoint set per
# active plugin (see tapcore.metadata.plugins), mounted at /api/v1/<plugin.mount>
# ---------------------------------------------------------------------------


class AmendRequest(BaseModel):
    table: str = Field(description="Target table (model-level name)")
    match: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Column equality filters selecting the rows to amend"
        " (empty = every row of the document in that table)",
    )
    values: dict = Field(
        description="Columns to set; each value is validated against the"
        " corresponding pydantic model field"
    )


def build_metadata_router(plugin: MetadataPlugin) -> APIRouter:
    """The JSON endpoint set for one metadata domain."""
    domain = APIRouter(prefix=f"/{plugin.mount}", tags=[f"metadata:{plugin.name}"])
    root = plugin.tables[0]
    id_column = root.id_column
    # "projects" -> "project", for readable 404 messages
    resource = root.name[:-1] if root.name.endswith("s") else root.name

    async def ingest_endpoint(document):
        with pool().connection() as conn:
            counts = ingest.ingest_document(conn, plugin, document)
        root_id = getattr(document, id_column)
        return {
            "status": "ingested",
            id_column: root_id,
            "rows": counts,
            "query_hint": (
                f"SELECT * FROM {root.qualified} WHERE {id_column} = "
                f"'{root_id}' (via /tap/sync or /api/v1/query)"
            ),
        }

    # the plugin's root model is only known at runtime: FastAPI picks the
    # request-body validator up from the endpoint's annotations
    ingest_endpoint.__annotations__ = {"document": plugin.model}
    ingest_endpoint.__doc__ = (
        f"Validate (with the {plugin.model.__name__} model) and store a document.\n\n"
        f"The payload is flattened into the {plugin.sql_schema}.* tables generated"
        " from the same model; re-posting upserts (idempotent)."
    )
    domain.post("", status_code=201, dependencies=[Depends(require("metadata.ingest"))])(
        ingest_endpoint
    )

    @domain.get("")
    async def list_endpoint():
        with pool().connection() as conn:
            summaries = ingest.list_documents(conn, plugin)
        return {plugin.root_table: summaries}

    @domain.get("/{root_id}")
    async def fetch_endpoint(root_id: str):
        with pool().connection() as conn:
            document = ingest.fetch_document(conn, plugin, root_id)
        if document is None:
            raise NotFoundError(f"{resource} {root_id} not found")
        return document

    @domain.delete("/{root_id}", dependencies=[Depends(require("metadata.delete"))])
    async def delete_endpoint(root_id: str, request: Request):
        """Delete a document; generated foreign keys cascade to every child row."""
        with pool().connection() as conn:
            deleted = ingest.delete_document(conn, plugin, root_id, actor=owner_of(request))
        if not deleted:
            raise NotFoundError(f"{resource} {root_id} not found")
        return {"status": "deleted", id_column: root_id}

    @domain.patch("/{root_id}", dependencies=[Depends(require("metadata.amend"))])
    async def amend_endpoint(root_id: str, body: AmendRequest):
        """Amend already-ingested rows — e.g. backfill a column added by a
        newer data-model release — without re-sending the whole document.

        Values are validated field-by-field with the plugin's models and the
        update is always scoped to the given root document.
        """
        with pool().connection() as conn, conn.transaction():
            if ingest.fetch_document(conn, plugin, root_id) is None:
                raise NotFoundError(f"{resource} {root_id} not found")
            updated = ingest.amend_rows(
                conn, plugin, root_id, body.table, dict(body.match), dict(body.values)
            )
        return {
            "status": "amended",
            id_column: root_id,
            "table": body.table,
            "updated": updated,
        }

    return domain


for _plugin in active_plugins():
    router.include_router(build_metadata_router(_plugin))
