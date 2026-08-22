"""UWS 1.1 REST resources under {base}/async."""

import datetime
import os
import shutil

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from tapcore import uws
from tapcore.config import settings
from tapcore.db import pool
from tapcore.errors import NotFoundError, UsageError
from tapcore.upload import save_upload_sources
from tapcore.votable import error_votable

from .params import gather_params
from .query import prepare_query
from .uploads import gather_upload_files, parse_uploads, resolve_upload_sources

router = APIRouter()

XML = "application/xml"


def _job_url(job_id: str) -> str:
    return f"{settings.base_url}/async/{job_id}"


def _iso(dt) -> str:
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _queue(conn, job: dict) -> None:
    """Validate the job parameters and move it to QUEUED for the executor."""
    if job["phase"] not in ("PENDING", "HELD"):
        raise UsageError(f"cannot start job in phase {job['phase']}")
    prepared = prepare_query(job["parameters"])
    uws.update_job(conn, job["job_id"], phase="QUEUED", query_sql=prepared["sql"])


@router.get("")
async def job_list(request: Request):
    phases = [p.upper() for p in request.query_params.getlist("PHASE")]
    for phase in phases:
        if phase not in uws.ALL_PHASES:
            raise UsageError(f"unknown PHASE {phase}")
    last = request.query_params.get("LAST")
    if last is not None:
        try:
            last = int(last)
        except ValueError:
            raise UsageError("LAST must be a positive integer") from None
        if last < 1:
            raise UsageError("LAST must be a positive integer")
    with pool().connection() as conn:
        jobs = uws.list_jobs(conn, phases or None, last)
    return Response(uws.joblist_xml(jobs), media_type=XML)


@router.post("")
async def create_job(request: Request):
    params = await gather_params(request)
    phase = params.pop("PHASE", None)
    files = await gather_upload_files(request)
    sources = resolve_upload_sources(params.get("UPLOAD"), files)
    parse_uploads(sources)  # reject malformed uploads before storing the job
    with pool().connection() as conn:
        job = uws.create_job(conn, params)
        if sources:
            save_upload_sources(job["job_id"], sources)
        if phase and phase.upper() == "RUN":
            _queue(conn, job)
    return RedirectResponse(_job_url(job["job_id"]), status_code=303)


@router.get("/{job_id}")
async def job_summary(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    return Response(uws.job_xml(job), media_type=XML)


@router.post("/{job_id}")
async def job_action(job_id: str, request: Request):
    params = await gather_params(request)
    action = params.get("ACTION", "").upper()
    if action == "DELETE":
        return await delete_job(job_id)
    raise UsageError("POST to the job URI requires ACTION=DELETE")


@router.delete("/{job_id}")
async def delete_job(job_id: str):
    with pool().connection() as conn:
        uws.delete_job(conn, job_id)
    shutil.rmtree(uws.job_results_dir(job_id), ignore_errors=True)
    return RedirectResponse(f"{settings.base_url}/async", status_code=303)


@router.get("/{job_id}/phase")
async def get_phase(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    return PlainTextResponse(job["phase"])


@router.post("/{job_id}/phase")
async def post_phase(job_id: str, request: Request):
    params = await gather_params(request)
    phase = params.get("PHASE", "").upper()
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
        if phase == "RUN":
            _queue(conn, job)
        elif phase == "ABORT":
            if job["phase"] not in uws.FINAL_PHASES:
                uws.update_job(
                    conn,
                    job_id,
                    phase="ABORTED",
                    end_time=datetime.datetime.now(datetime.UTC),
                )
        else:
            raise UsageError("PHASE must be RUN or ABORT")
    return RedirectResponse(_job_url(job_id), status_code=303)


@router.get("/{job_id}/executionduration")
async def get_execution_duration(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    return PlainTextResponse(str(job["execution_duration"]))


@router.post("/{job_id}/executionduration")
async def post_execution_duration(job_id: str, request: Request):
    params = await gather_params(request)
    try:
        duration = int(params.get("EXECUTIONDURATION", ""))
    except ValueError:
        raise UsageError("EXECUTIONDURATION must be an integer number of seconds") from None
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
        if job["phase"] != "PENDING":
            raise UsageError("executionduration can only be set while the job is PENDING")
        uws.update_job(conn, job_id, execution_duration=max(0, duration))
    return RedirectResponse(_job_url(job_id), status_code=303)


@router.get("/{job_id}/destruction")
async def get_destruction(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    return PlainTextResponse(_iso(job["destruction"]))


@router.post("/{job_id}/destruction")
async def post_destruction(job_id: str, request: Request):
    params = await gather_params(request)
    raw = params.get("DESTRUCTION", "")
    try:
        when = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise UsageError("DESTRUCTION must be an ISO-8601 timestamp") from None
    with pool().connection() as conn:
        uws.update_job(conn, job_id, destruction=when)
    return RedirectResponse(_job_url(job_id), status_code=303)


@router.get("/{job_id}/quote")
async def get_quote(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    return PlainTextResponse(_iso(job["quote"]) if job["quote"] else "")


@router.get("/{job_id}/owner")
async def get_owner(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    return PlainTextResponse(job["owner_id"] or "")


@router.get("/{job_id}/parameters")
async def get_parameters(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    return Response(uws.job_xml(job), media_type=XML)


@router.post("/{job_id}/parameters")
async def post_parameters(job_id: str, request: Request):
    params = await gather_params(request)
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
        if job["phase"] != "PENDING":
            raise UsageError("parameters can only be updated while the job is PENDING")
        merged = dict(job["parameters"] or {})
        merged.update(params)
        uws.update_job(conn, job_id, parameters=merged)
    return RedirectResponse(_job_url(job_id), status_code=303)


@router.get("/{job_id}/results")
async def get_results(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    return Response(uws.job_xml(job), media_type=XML)


@router.get("/{job_id}/results/result")
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


@router.get("/{job_id}/error")
async def get_error(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    return Response(
        error_votable(job["error_message"] or "no error"),
        media_type="application/x-votable+xml",
    )
