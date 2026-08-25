"""Per-stage cost of one async job, measured against a local PostgreSQL.

Replays the executor's ``_execute_job_inner`` happy path stage by stage with
``perf_counter`` around each stage, for jobs that carry ``query_tables`` (the
current queue path) and for jobs that do not (the fallback that re-parses the
stored SQL), then cross-checks each stage sum against the wall time of the
real ``worker.execute_job()``. This is the measurement behind the package 20
finding in docs/roadmap.md.

Run it against the docker-compose database (``docker compose up db``):

    TAP_RESULTS_DIR=/tmp/tap-results uv run python tests/performance/profile_async_job.py [reps]

The stage list mirrors ``_execute_job_inner`` by hand, so if that function
gains or loses a stage this script must follow; the cross-check line is what
catches a drift — the stage sum and the real wall time should agree within a
few percent.
"""

import contextlib
import datetime
import json
import os
import statistics
import sys
import time

from egernia_core import uws
from egernia_core.config import settings
from egernia_core.db import StreamedRows
from egernia_core.db import connection as db_connection
from egernia_core.observability import tag_sql
from egernia_core.query.adql import apply_maxrec, touched_tables, translate
from egernia_core.query.results import (
    RowLimiter,
    columns_from_cursor,
    stream,
    tap_schema_metadata,
)
from egernia_core.query.votable import normalize_format
from egernia_executor import worker

ADQL = (
    "SELECT source_id, ra, dec, flux_int FROM ska.continuum_sources "
    "WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 62.3, -65.5, 1.0))"
)


@contextlib.contextmanager
def _stage(record, name):
    started = time.perf_counter()
    try:
        yield
    finally:
        record[name] = record.get(name, 0.0) + (time.perf_counter() - started)


def _insert_queued(translation, with_tables):
    job_id = uws.new_job_id()
    now = datetime.datetime.now(datetime.UTC)
    with db_connection() as conn:
        conn.execute(
            "INSERT INTO uws.jobs (job_id, phase, creation_time, execution_duration,"
            " destruction, parameters, query_sql, query_tables)"
            " VALUES (%s, 'QUEUED', %s, 600, %s, %s::jsonb, %s, %s)",
            (
                job_id,
                now,
                now + datetime.timedelta(hours=1),
                json.dumps({"QUERY": ADQL, "RESPONSEFORMAT": "votable"}),
                translation.sql,
                sorted(translation.tables) if with_tables else None,
            ),
        )
    return job_id


def _run_staged(job, use_stored):
    """The executor's happy path, stage-timed. Mirrors _execute_job_inner."""
    timings = {}
    job_id = job["job_id"]
    params = job["parameters"] or {}
    with _stage(timings, "params+tag"):
        maxrec = min(int(params.get("MAXREC", settings.default_maxrec)), settings.hard_maxrec)
        fmt_key, mime, ext = normalize_format(params.get("RESPONSEFORMAT") or params.get("FORMAT"))
        sql = uws.job_query_tag(job_id) + tag_sql(apply_maxrec(job["query_sql"], maxrec))
    with _stage(timings, "tables"):
        tables = set(job["query_tables"]) if use_stored else touched_tables(job["query_sql"])
    with _stage(timings, "mkdir"):
        result_dir = uws.job_results_dir(job_id)
        os.makedirs(result_dir, exist_ok=True)
        result_path = os.path.join(result_dir, f"result.{ext}")

    result_size = 0
    with contextlib.ExitStack() as stack:
        with _stage(timings, "conn checkout+begin"):
            conn = stack.enter_context(db_connection())
            stack.enter_context(conn.transaction())
        with _stage(timings, "tap_schema_metadata"):
            tap_meta = tap_schema_metadata(conn, tables)
        with _stage(timings, "timeout+backend_pid"):
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(job["execution_duration"]) * 1000),),
            )
            pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        with _stage(timings, "publish pid (side conn)"), db_connection() as side:
            side.execute(
                "UPDATE uws.jobs SET backend_pid = %s WHERE job_id = %s AND phase = 'EXECUTING'",
                (pid, job_id),
            )
        with _stage(timings, "jit+role"):
            conn.execute("SET LOCAL jit = off")
            conn.execute(f"SET LOCAL ROLE {settings.query_role}")
        with _stage(timings, "watchdog start"):
            stack.enter_context(worker._AbortWatchdog(job_id, pid))
        with _stage(timings, "execute (first chunk)"), conn.cursor() as cur:
            rows = StreamedRows(cur, sql, chunk_rows=5000)
            stack.enter_context(contextlib.closing(rows))
            columns = columns_from_cursor(cur.description, tap_meta)
            limiter = RowLimiter(rows, maxrec)
        with _stage(timings, "serialize+write"), open(result_path, "wb") as fh:
            for chunk in stream(columns, limiter, fmt_key):
                result_size += fh.write(chunk)
        with _stage(timings, "watchdog stop + commit + conn return"):
            stack.close()

    with _stage(timings, "finalize (COMPLETED update)"), db_connection() as conn:
        conn.execute(
            "UPDATE uws.jobs SET phase = 'COMPLETED', end_time = %s,"
            " result_mime = %s, result_size = %s, backend_pid = NULL"
            " WHERE job_id = %s AND phase = 'EXECUTING'",
            (datetime.datetime.now(datetime.UTC), mime, result_size, job_id),
        )
    return timings


def main() -> None:
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    os.makedirs(settings.results_dir, exist_ok=True)
    worker._ensure_job_columns(attempts=1)
    translation = translate(ADQL)

    for _ in range(5):  # warm the pool and the caches
        _insert_queued(translation, with_tables=True)
        worker.execute_job(worker.claim_job())

    results = {}
    for label, with_tables in (("stored tables (new)", True), ("per-job parse (old)", False)):
        samples, claims = [], []
        for _ in range(reps):
            _insert_queued(translation, with_tables)
            started = time.perf_counter()
            job = worker.claim_job()
            claims.append(time.perf_counter() - started)
            samples.append(_run_staged(job, use_stored=with_tables))
        stages = {name: statistics.median(s[name] for s in samples) for name in samples[0]}
        stages["claim"] = statistics.median(claims)
        results[label] = stages

    # cross-check: the real execute_job, end to end
    for label, with_tables in (("stored tables (new)", True), ("per-job parse (old)", False)):
        walls = []
        for _ in range(reps):
            _insert_queued(translation, with_tables)
            job = worker.claim_job()
            started = time.perf_counter()
            worker.execute_job(job)
            walls.append(time.perf_counter() - started)
        results[label]["REAL execute_job wall"] = statistics.median(walls)

    for label, stages in results.items():
        print(f"\n=== {label} (median of {reps}, ms) ===")
        total = 0.0
        for name, seconds in stages.items():
            if name == "REAL execute_job wall":
                continue
            total += seconds
            print(f"  {name:34s} {seconds * 1e3:8.3f}")
        print(f"  {'stage sum (claim..finalize)':34s} {total * 1e3:8.3f}")
        wall = stages["REAL execute_job wall"]
        print(f"  {'real execute_job wall (no claim)':34s} {wall * 1e3:8.3f}")

    with db_connection() as conn:  # clean up the jobs this run created
        conn.execute("DELETE FROM uws.jobs WHERE parameters->>'QUERY' = %s", (ADQL,))


if __name__ == "__main__":
    main()
