"""UWS 1.1 REST resources under {base}/async."""

import shutil

from egernia_core import uws, uws_xml
from egernia_core.config import base_url, settings
from egernia_core.db import connection as db_connection
from egernia_core.errors import UsageError
from egernia_core.query.upload import save_upload_sources
from egernia_core.query.votable import error_votable
from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ..auth import owner_of, require
from ..queries.jobs import (
    fetch_job,
    parse_iso,
    parse_job_filters,
    queue_job,
    result_file_response,
    run_or_abort,
    wait_for_phase,
)
from ..queries.params import gather_params
from ..queries.query import prepare_query
from ..queries.uploads import gather_upload_sources, parse_uploads

# The prefix belongs on the router, not on the include. FastAPI keeps included
# routers nested (fastapi.routing._IncludedRouter) rather than flattening them,
# and Starlette then reports the innermost matched route — so a prefix passed to
# include_router is absent from `request.scope["route"].path`, which is what the
# authorisation layer sends the Permissions API as the route being asked about.
# Declared here it is present, and the value agrees with the OpenAPI document.
router = APIRouter(prefix="/tap/async")

XML = "application/xml"


def _job_url(job_id: str) -> str:
    """The canonical URL of a job.

    Callers pass the id as stored in uws.jobs — never the raw path segment —
    so only a server-generated id can reach a Location header.
    """
    return f"{base_url()}/async/{job_id}"


def _parse_wait(request: Request) -> tuple[int, str | None]:
    """UWS 1.1 blocking parameters: WAIT seconds (-1 = server maximum) and
    the optional PHASE the client believes the job is in."""
    raw = request.query_params.get("WAIT")
    if raw is None:
        return 0, None
    try:
        wait = int(raw)
    except ValueError:
        raise UsageError("WAIT must be an integer number of seconds or -1") from None
    if wait < -1:
        raise UsageError("WAIT must be >= -1")
    if wait == -1:
        wait = settings.wait_max_s
    phase = request.query_params.get("PHASE")
    if phase is not None:
        phase = phase.upper()
        if phase not in uws.ALL_PHASES:
            raise UsageError(f"unknown PHASE {phase}")
    return min(wait, settings.wait_max_s), phase


async def _get_job_waiting(job_id: str, request: Request) -> dict:
    wait_s, expected = _parse_wait(request)
    return await wait_for_phase(job_id, wait_s, expected)


@router.get("")
def job_list(request: Request):
    phases, last, since = parse_job_filters(
        [p.upper() for p in request.query_params.getlist("PHASE")],
        request.query_params.get("LAST"),
        request.query_params.get("AFTER"),
    )
    with db_connection() as conn:
        jobs = uws.list_jobs(conn, phases, last, since)
    return Response(uws_xml.joblist_xml(jobs), media_type=XML)


@router.post("", dependencies=[Depends(require("jobs.create"))])
async def create_job(request: Request):
    params = await gather_params(request)
    phase = params.pop("PHASE", None)
    sources = await gather_upload_sources(request, params)
    await run_in_threadpool(parse_uploads, sources)  # reject before storing the job
    # One flag decides both the translation and the queueing, so the prepared
    # query cannot be missing where it is used. Not an assert: those vanish
    # under -O, which is exactly when a mistake here would matter.
    run_now = bool(phase and phase.upper() == "RUN")
    # translated off the event loop, before the connection is held
    prepared = await run_in_threadpool(prepare_query, params) if run_now else None

    def store_job():
        with db_connection() as conn:
            job = uws.create_job(conn, params, owner_id=owner_of(request))
            if sources:
                save_upload_sources(job["job_id"], sources)
            if run_now and prepared is not None:
                queue_job(conn, job, prepared)
            return job

    job = await run_in_threadpool(store_job)
    return RedirectResponse(_job_url(job["job_id"]), status_code=303)


@router.get("/{job_id}")
async def job_summary(job_id: str, request: Request):
    job = await _get_job_waiting(job_id, request)
    return Response(uws_xml.job_xml(job), media_type=XML)


@router.post("/{job_id}", dependencies=[Depends(require("jobs.delete"))])
async def job_action(job_id: str, request: Request):
    params = await gather_params(request)
    action = params.get("ACTION", "").upper()
    if action == "DELETE":
        return await run_in_threadpool(delete_job, job_id)
    raise UsageError("POST to the job URI requires ACTION=DELETE")


@router.delete("/{job_id}", dependencies=[Depends(require("jobs.delete"))])
def delete_job(job_id: str):
    with db_connection() as conn:
        uws.delete_job(conn, job_id)
    shutil.rmtree(uws.job_results_dir(job_id), ignore_errors=True)
    return RedirectResponse(f"{base_url()}/async", status_code=303)


@router.get("/{job_id}/phase")
async def get_phase(job_id: str, request: Request):
    job = await _get_job_waiting(job_id, request)
    return PlainTextResponse(job["phase"])


@router.post("/{job_id}/phase", dependencies=[Depends(require("jobs.mutate"))])
async def post_phase(job_id: str, request: Request):
    params = await gather_params(request)
    job = await run_or_abort(job_id, params.get("PHASE", "").upper())
    return RedirectResponse(_job_url(job["job_id"]), status_code=303)


@router.get("/{job_id}/executionduration")
def get_execution_duration(job_id: str):
    job = fetch_job(job_id)
    return PlainTextResponse(str(job["execution_duration"]))


@router.post("/{job_id}/executionduration", dependencies=[Depends(require("jobs.mutate"))])
async def post_execution_duration(job_id: str, request: Request):
    params = await gather_params(request)
    try:
        duration = int(params.get("EXECUTIONDURATION", ""))
    except ValueError:
        raise UsageError("EXECUTIONDURATION must be an integer number of seconds") from None

    def update_duration():
        with db_connection() as conn:
            job = uws.get_job(conn, job_id)
            if job["phase"] != "PENDING":
                raise UsageError("executionduration can only be set while the job is PENDING")
            uws.update_job(conn, job_id, execution_duration=max(0, duration))
            return job

    job = await run_in_threadpool(update_duration)
    return RedirectResponse(_job_url(job["job_id"]), status_code=303)


@router.get("/{job_id}/destruction")
def get_destruction(job_id: str):
    job = fetch_job(job_id)
    return PlainTextResponse(uws.iso_utc(job["destruction"]) or "")


@router.post("/{job_id}/destruction", dependencies=[Depends(require("jobs.mutate"))])
async def post_destruction(job_id: str, request: Request):
    params = await gather_params(request)
    when = parse_iso(params.get("DESTRUCTION", ""), "DESTRUCTION")

    def update_destruction():
        with db_connection() as conn:
            job = uws.get_job(conn, job_id)
            uws.update_job(conn, job_id, destruction=when)
            return job

    job = await run_in_threadpool(update_destruction)
    return RedirectResponse(_job_url(job["job_id"]), status_code=303)


@router.get("/{job_id}/quote")
def get_quote(job_id: str):
    job = fetch_job(job_id)
    return PlainTextResponse(uws.iso_utc(job["quote"]) or "")


@router.get("/{job_id}/owner")
def get_owner(job_id: str):
    job = fetch_job(job_id)
    return PlainTextResponse(job["owner_id"] or "")


@router.get("/{job_id}/parameters")
def get_parameters(job_id: str):
    job = fetch_job(job_id)
    return Response(uws_xml.job_xml(job), media_type=XML)


@router.post("/{job_id}/parameters", dependencies=[Depends(require("jobs.mutate"))])
async def post_parameters(job_id: str, request: Request):
    params = await gather_params(request)

    def update_parameters():
        with db_connection() as conn:
            job = uws.get_job(conn, job_id)
            if job["phase"] != "PENDING":
                raise UsageError("parameters can only be updated while the job is PENDING")
            merged = dict(job["parameters"] or {})
            merged.update(params)
            uws.update_job(conn, job_id, parameters=merged)
            return job

    job = await run_in_threadpool(update_parameters)
    return RedirectResponse(_job_url(job["job_id"]), status_code=303)


@router.get("/{job_id}/results")
def get_results(job_id: str):
    job = fetch_job(job_id)
    return Response(uws_xml.job_xml(job), media_type=XML)


@router.get("/{job_id}/results/result")
def get_result(job_id: str):
    job = fetch_job(job_id)
    return result_file_response(job, job_id)


@router.get("/{job_id}/error")
def get_error(job_id: str):
    job = fetch_job(job_id)
    return Response(
        error_votable(job["error_message"] or "no error"),
        media_type="application/x-votable+xml",
    )
