"""UWS 1.1 REST resources under {base}/async."""

import asyncio
import datetime
import os
import shutil
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from tapcore import uws
from tapcore.config import settings
from tapcore.db import pool
from tapcore.errors import NotFoundError, UsageError
from tapcore.query.upload import save_upload_sources
from tapcore.query.votable import error_votable

from ..auth import owner_of, require
from ..queries.params import gather_params
from ..queries.query import prepare_query
from ..queries.uploads import gather_upload_files, parse_uploads, resolve_upload_sources

router = APIRouter()

XML = "application/xml"


def _job_url(job_id: str) -> str:
    """The canonical URL of a job.

    Callers pass the id as stored in uws.jobs — never the raw path segment —
    so only a server-generated id can reach a Location header.
    """
    return f"{settings.base_url}/async/{job_id}"


def _iso(dt) -> str:
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _queue(conn, job: dict, prepared: dict | None = None) -> None:
    """Validate the job parameters and move it to QUEUED for the executor.

    ``prepared`` lets a caller that has already translated these parameters
    pass the result in rather than pay a second ADQL parse.
    """
    if job["phase"] not in ("PENDING", "HELD"):
        raise UsageError(f"cannot start job in phase {job['phase']}")
    prepared = prepared or prepare_query(job["parameters"])
    uws.update_job(conn, job["job_id"], phase="QUEUED", query_sql=prepared["sql"])


WAIT_POLL_S = 1.0


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
    """Fetch the job, blocking per UWS 1.1 WAIT semantics.

    Blocks while the job is in an active phase and its phase equals the
    reference phase (the PHASE parameter, or the phase first observed),
    until the phase changes or the wait time expires.
    """
    wait_s, expected = _parse_wait(request)
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    if wait_s <= 0 or job["phase"] in uws.FINAL_PHASES:
        return job
    reference = expected or job["phase"]
    deadline = time.monotonic() + wait_s
    while job["phase"] == reference and time.monotonic() < deadline:
        await asyncio.sleep(min(WAIT_POLL_S, max(0.0, deadline - time.monotonic())))
        with pool().connection() as conn:
            job = uws.get_job(conn, job_id)
    return job


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
    after = request.query_params.get("AFTER")
    if after is not None:
        try:
            after = datetime.datetime.fromisoformat(after.replace("Z", "+00:00"))
        except ValueError:
            raise UsageError("AFTER must be an ISO-8601 timestamp") from None
    with pool().connection() as conn:
        jobs = uws.list_jobs(conn, phases or None, last, after)
    return Response(uws.joblist_xml(jobs), media_type=XML)


@router.post("", dependencies=[Depends(require("jobs.create"))])
async def create_job(request: Request):
    params = await gather_params(request)
    phase = params.pop("PHASE", None)
    files = await gather_upload_files(request)
    sources = resolve_upload_sources(params.get("UPLOAD"), files)
    parse_uploads(sources)  # reject malformed uploads before storing the job
    with pool().connection() as conn:
        job = uws.create_job(conn, params, owner_id=owner_of(request))
        if sources:
            save_upload_sources(job["job_id"], sources)
        if phase and phase.upper() == "RUN":
            _queue(conn, job)
    return RedirectResponse(_job_url(job["job_id"]), status_code=303)


@router.get("/{job_id}")
async def job_summary(job_id: str, request: Request):
    job = await _get_job_waiting(job_id, request)
    return Response(uws.job_xml(job), media_type=XML)


@router.post("/{job_id}", dependencies=[Depends(require("jobs.delete"))])
async def job_action(job_id: str, request: Request):
    params = await gather_params(request)
    action = params.get("ACTION", "").upper()
    if action == "DELETE":
        return await delete_job(job_id)
    raise UsageError("POST to the job URI requires ACTION=DELETE")


@router.delete("/{job_id}", dependencies=[Depends(require("jobs.delete"))])
async def delete_job(job_id: str):
    with pool().connection() as conn:
        uws.delete_job(conn, job_id)
    shutil.rmtree(uws.job_results_dir(job_id), ignore_errors=True)
    return RedirectResponse(f"{settings.base_url}/async", status_code=303)


@router.get("/{job_id}/phase")
async def get_phase(job_id: str, request: Request):
    job = await _get_job_waiting(job_id, request)
    return PlainTextResponse(job["phase"])


@router.post("/{job_id}/phase", dependencies=[Depends(require("jobs.mutate"))])
async def post_phase(job_id: str, request: Request):
    params = await gather_params(request)
    phase = params.get("PHASE", "").upper()
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
        if phase == "RUN":
            _queue(conn, job)
        elif phase == "ABORT":
            uws.abort_job(conn, job)
        else:
            raise UsageError("PHASE must be RUN or ABORT")
    return RedirectResponse(_job_url(job["job_id"]), status_code=303)


@router.get("/{job_id}/executionduration")
async def get_execution_duration(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    return PlainTextResponse(str(job["execution_duration"]))


@router.post("/{job_id}/executionduration", dependencies=[Depends(require("jobs.mutate"))])
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
    return RedirectResponse(_job_url(job["job_id"]), status_code=303)


@router.get("/{job_id}/destruction")
async def get_destruction(job_id: str):
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
    return PlainTextResponse(_iso(job["destruction"]))


@router.post("/{job_id}/destruction", dependencies=[Depends(require("jobs.mutate"))])
async def post_destruction(job_id: str, request: Request):
    params = await gather_params(request)
    raw = params.get("DESTRUCTION", "")
    try:
        when = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise UsageError("DESTRUCTION must be an ISO-8601 timestamp") from None
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
        uws.update_job(conn, job_id, destruction=when)
    return RedirectResponse(_job_url(job["job_id"]), status_code=303)


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


@router.post("/{job_id}/parameters", dependencies=[Depends(require("jobs.mutate"))])
async def post_parameters(job_id: str, request: Request):
    params = await gather_params(request)
    with pool().connection() as conn:
        job = uws.get_job(conn, job_id)
        if job["phase"] != "PENDING":
            raise UsageError("parameters can only be updated while the job is PENDING")
        merged = dict(job["parameters"] or {})
        merged.update(params)
        uws.update_job(conn, job_id, parameters=merged)
    return RedirectResponse(_job_url(job["job_id"]), status_code=303)


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
