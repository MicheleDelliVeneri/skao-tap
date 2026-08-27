"""Modern JSON interface for machine-to-machine use, mounted at /api/v1.

This is a facade over the same engine as the IVOA TAP endpoints: the same
ADQL translation, TAP_SCHEMA publication checks, MAXREC limits, and UWS job
store — with JSON requests/responses (OpenAPI-documented via FastAPI)
instead of the XML the VO standards mandate. Standard VO clients keep using
/tap; services and pipelines can use this API.

It also publishes the active metadata-domain plugins (egernia_core.metadata.plugins):
each plugin's documents are validated with its own pydantic models and
stored in tables generated from those same models — so ingested metadata
is instantly queryable via both this API and TAP/ADQL. The observatory
data product domain (egernia_api.plugins.odp, srcnet schema) and the software
discovery domain (egernia_api.plugins.software) ship built in; third-party model
packages register through the egernia.models entry-point group.
"""

import logging
import shutil

from egernia_core import uws
from egernia_core.config import base_url, settings
from egernia_core.db import connection as db_connection
from egernia_core.errors import NotFoundError, UsageError
from egernia_core.metadata import ingest
from egernia_core.metadata.plugins import MetadataPlugin, active_plugins
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from ..auth import auth_summary, gated, owner_of, require
from ..queries.query import prepare_query, run_sync
from .uws_api import (
    fetch_job,
    parse_job_filters,
    queue_job,
    result_file_response,
    run_or_abort,
    wait_for_phase,
)

API_PREFIX = "/api/v1"
router = APIRouter(prefix=API_PREFIX, tags=["json-api"])
log = logging.getLogger("egernia_api")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    query: str = Field(description="ADQL query", min_length=1)
    lang: str = Field(default="ADQL", description="Query language (ADQL, ADQL-2.0, or ADQL-2.1)")
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
    # off the event loop, for the same reason as /tap/sync
    chunks, mime = await run_in_threadpool(run_sync, prepared)
    return StreamingResponse(iterate_in_threadpool(chunks), media_type=mime)


# ---------------------------------------------------------------------------
# Jobs (JSON facade over the shared UWS job store)
# ---------------------------------------------------------------------------


def _api_base() -> str:
    """The /api/v1 base derived from the TAP base URL (…/tap -> …/api/v1)."""
    root = base_url().rsplit("/tap", 1)[0]
    return f"{root}/api/v1"


def _job_json(job: dict) -> dict:
    body = {
        "job_id": job["job_id"],
        "phase": job["phase"],
        "run_id": job["run_id"],
        "owner_id": job["owner_id"],
        "creation_time": uws.iso_utc(job["creation_time"]),
        "start_time": uws.iso_utc(job["start_time"]),
        "end_time": uws.iso_utc(job["end_time"]),
        "execution_duration": job["execution_duration"],
        "destruction": uws.iso_utc(job["destruction"]),
        "parameters": job["parameters"],
        "urls": {
            "job": f"{_api_base()}/jobs/{job['job_id']}",
            "uws": f"{base_url()}/async/{job['job_id']}",
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


@router.post("/jobs", status_code=201, dependencies=[Depends(require("jobs.create"))])
async def create_job(body: JobRequest, request: Request):
    params = _tap_params(body, fmt=body.format)
    if body.run_id:
        params["RUNID"] = body.run_id
    # validate before storing, unlike the lenient UWS XML flow — and keep the
    # result, so running the job now does not re-translate the same query
    prepared = await run_in_threadpool(prepare_query, params)
    with db_connection() as conn:
        job = uws.create_job(conn, params, owner_id=owner_of(request))
        if body.run:
            queue_job(conn, job, prepared)
        return _job_json(uws.get_job(conn, job["job_id"]))


@router.get("/jobs")
async def list_jobs(phase: str | None = None, last: int | None = None, after: str | None = None):
    phases = [p.strip().upper() for p in phase.split(",")] if phase else []
    phases, last, since = parse_job_filters(phases, last, after)
    with db_connection() as conn:
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
    return _job_json(await wait_for_phase(job_id, min(wait or 0, settings.wait_max_s)))


class PhaseRequest(BaseModel):
    phase: str = Field(description="RUN or ABORT")


@router.post("/jobs/{job_id}/phase", dependencies=[Depends(require("jobs.mutate"))])
async def post_phase(job_id: str, body: PhaseRequest):
    return _job_json(await run_or_abort(job_id, body.phase.upper()))


@router.delete("/jobs/{job_id}", status_code=204, dependencies=[Depends(require("jobs.delete"))])
async def delete_job(job_id: str):
    with db_connection() as conn:
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
    return result_file_response(fetch_job(job_id), job_id)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@router.get("/tables")
async def tables():
    """JSON rendering of TAP_SCHEMA (machine-friendly alternative to VOSI XML)."""
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT t.schema_name, t.table_name, t.description, t.utype,
                   jsonb_agg(jsonb_build_object(
                       'name', c.column_name, 'datatype', c.datatype,
                       'unit', c.unit, 'ucd', c.ucd, 'utype', c.utype,
                       'xtype', c.xtype, 'description', c.description)
                       ORDER BY c.column_index)
            FROM tap_schema.tables t
            JOIN tap_schema.columns c ON c.table_name = t.table_name
            GROUP BY t.schema_name, t.table_name, t.description, t.utype, t.table_index
            ORDER BY t.schema_name, t.table_index
            """
        ).fetchall()
    return {
        "tables": [
            {"schema": r[0], "name": r[1], "description": r[2], "utype": r[3], "columns": r[4]}
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Metadata-domain plugins: one ingest/list/fetch/amend endpoint set per
# active plugin (see egernia_core.metadata.plugins), mounted at /api/v1/<plugin.mount>
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
    # The full prefix, and included on the app rather than on `router` below.
    # A router nested inside a prefixed one loses the outer prefix from
    # `request.scope["route"].path` — see the note in uws_api.py — and that
    # value is what the authorisation layer asks the Permissions API about.
    domain = APIRouter(prefix=f"{API_PREFIX}/{plugin.mount}", tags=[f"metadata:{plugin.name}"])
    root = plugin.tables[0]
    id_column = root.id_column
    # "projects" -> "project", for readable 404 messages
    resource = root.name[:-1] if root.name.endswith("s") else root.name

    async def ingest_endpoint(document):
        with db_connection() as conn:
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
        with db_connection() as conn:
            summaries = ingest.list_documents(conn, plugin)
        return {plugin.root_table: summaries}

    @domain.get("/{root_id}")
    async def fetch_endpoint(root_id: str):
        with db_connection() as conn:
            document = ingest.fetch_document(conn, plugin, root_id)
        if document is None:
            raise NotFoundError(f"{resource} {root_id} not found")
        return document

    @domain.delete("/{root_id}", dependencies=[Depends(require("metadata.delete"))])
    async def delete_endpoint(root_id: str, request: Request):
        """Delete a document; generated foreign keys cascade to every child row."""
        with db_connection() as conn:
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
        with db_connection() as conn, conn.transaction():
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


# Included on the app by main.py, not on `router`: nesting them inside a
# prefixed router is what hid `/api/v1` from the route the authorisation layer
# reports. The URLs are identical either way.
metadata_routers = [build_metadata_router(_plugin) for _plugin in active_plugins()]
