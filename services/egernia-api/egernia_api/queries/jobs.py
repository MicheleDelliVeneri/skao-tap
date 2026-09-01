"""Protocol-neutral job helpers shared by the UWS (XML) and JSON facades.

Everything here is HTTP-shaped but rendering-free: both endpoint modules
call these and then answer in their own format, so neither imports the
other's route implementation.
"""

import asyncio
import datetime
import os
import time

from egernia_core import uws
from egernia_core.db import connection as db_connection
from egernia_core.errors import NotFoundError, QueryParseError, TAPError, UsageError
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .query import prepare_query

WAIT_POLL_S = 1.0


def fetch_job(job_id: str) -> dict:
    """The job, on a connection held only for the read. The mutating
    endpoints keep their own ``with`` block on purpose: they read and update
    inside one transaction, so their read must not become this one."""
    with db_connection() as conn:
        return uws.get_job(conn, job_id)


def parse_iso(raw: str, param: str) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise UsageError(f"{param} must be an ISO-8601 timestamp") from None


def queue_job(conn, job: dict, prepared: dict) -> None:
    """Move a job to QUEUED, using an already-translated query.

    ``prepared`` is required rather than optional: translating here would put
    an ADQL parse back on the event loop, since every caller is an async
    handler. Required means a future caller cannot reintroduce that quietly.
    """
    updated = uws.update_job(
        conn,
        job["job_id"],
        expected_phases=("PENDING", "HELD"),
        phase="QUEUED",
        query_sql=prepared["sql"],
        # from the parse the API already did, so the executor never parses
        query_tables=sorted(prepared["tables"]),
    )
    if not updated:
        current = uws.get_job(conn, job["job_id"])
        raise UsageError(f"cannot start job in phase {current['phase']}")


async def wait_for_phase(job_id: str, wait_s: int, expected: str | None = None) -> dict:
    """Fetch the job, blocking per UWS 1.1 WAIT semantics.

    Blocks while the job is in an active phase and its phase equals the
    reference phase (``expected``, or the phase first observed), until the
    phase changes or the wait time expires.
    """
    job = await run_in_threadpool(fetch_job, job_id)
    if wait_s <= 0 or job["phase"] in uws.FINAL_PHASES:
        return job
    reference = expected or job["phase"]
    deadline = time.monotonic() + wait_s
    while job["phase"] == reference and time.monotonic() < deadline:
        await asyncio.sleep(min(WAIT_POLL_S, max(0.0, deadline - time.monotonic())))
        job = await run_in_threadpool(fetch_job, job_id)
    return job


def parse_job_filters(
    phases: list[str], last, after: str | None
) -> tuple[list[str] | None, int | None, datetime.datetime | None]:
    """Validate the UWS 1.1 job-list filters (PHASE / LAST / AFTER)."""
    for phase in phases:
        if phase not in uws.ALL_PHASES:
            raise UsageError(f"unknown PHASE {phase}")
    if last is not None:
        try:
            last = int(last)
        except ValueError:
            raise UsageError("LAST must be a positive integer") from None
        if last < 1:
            raise UsageError("LAST must be a positive integer")
    since = parse_iso(after, "AFTER") if after is not None else None
    return phases or None, last, since


async def run_or_abort(job_id: str, phase: str) -> dict:
    """Apply a UWS phase command (RUN or ABORT) and return the job.

    A RUN whose query fails to translate does not refuse the request: UWS
    problems with the job live *in* the job, so the caller gets its 303 and
    finds the bad ADQL in the ERROR phase (taplint E-Q*-DFIO). Service
    faults still raise — they are not properties of the job.
    """
    prepared = None
    prepare_error: TAPError | None = None
    if phase == "RUN":
        # the job's own parameters, translated off the event loop
        stored = await run_in_threadpool(fetch_job, job_id)
        try:
            prepared = await run_in_threadpool(prepare_query, stored["parameters"])
        except (UsageError, QueryParseError) as exc:
            prepare_error = exc

    def apply_phase():
        with db_connection() as conn:
            job = uws.get_job(conn, job_id)
            if prepared is not None:  # set exactly when the phase is RUN
                queue_job(conn, job, prepared)
            elif prepare_error is not None:
                uws.update_job(
                    conn,
                    job_id,
                    expected_phases=("PENDING", "HELD"),
                    phase="ERROR",
                    end_time=datetime.datetime.now(datetime.UTC),
                    error_type="fatal",
                    error_message=prepare_error.message,
                )
            elif phase == "ABORT":
                uws.abort_job(conn, job)
            else:
                raise UsageError("PHASE must be RUN or ABORT")
            return uws.get_job(conn, job_id)

    return await run_in_threadpool(apply_phase)


def result_file_response(job: dict, job_id: str) -> FileResponse:
    """The job's result file, or NotFoundError if it has none (yet)."""
    if job["phase"] != "COMPLETED":
        raise NotFoundError(f"job {job_id} has no result (phase {job['phase']})")
    result_dir = uws.job_results_dir(job_id)
    try:
        names = os.listdir(result_dir) if os.path.isdir(result_dir) else []
    except OSError:
        names = []
    for name in names:
        if name.startswith("result."):
            return FileResponse(
                os.path.join(result_dir, name),
                media_type=job["result_mime"] or "application/octet-stream",
            )
    raise NotFoundError(f"result file for job {job_id} not found")
