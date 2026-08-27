"""Coarse timing guards on a deployed egernia.

Not a benchmark, and deliberately not written like one. A shared integration
cluster has neighbours, so any number measured here is a fact about the cluster
that afternoon rather than about the service — which is why every budget below
is an order of magnitude above the expected time rather than a percentage over
it. These catch the failures that change the shape of a query plan:

  - a GiST footprint index that the seeder dropped and never rebuilt, turning
    a cone search into a sequential scan over every row;
  - a b-tree lost in a migration, turning a keyed lookup into the same;
  - a connection pool sized so requests queue rather than run;
  - a result path that buffers the whole table before writing a byte.

Each of those costs a factor, not a few percent, so a loose budget catches them
and survives a busy cluster. A regression small enough to slip past these is
one only a benchmark on dedicated hardware could honestly detect.

Every budget is overridable, because "generous" depends on the hardware the
environment happens to run on.
"""

from __future__ import annotations

import csv
import io
import os
import time

import pytest
from conftest import QUERY_TIMEOUT_S, sync_query


def _budget(name: str, default: int) -> int:
    return int(os.getenv(f"EGERNIA_BUDGET_{name}", str(default)))


# A keyed lookup and a TOP-N read: both should be index-served and effectively
# instant. The budget is for a cluster under load, not for the query.
POINT_BUDGET_S = _budget("POINT_S", 20)
# A cone search over the seeded footprints. Slower than a point lookup because
# it is a spatial index probe plus a recheck, still nowhere near a scan.
CONE_BUDGET_S = _budget("CONE_S", 45)
# A full-table aggregate, which no index helps: this one is genuinely expected
# to take seconds. Bounded by the deployment's own syncTimeoutSeconds (120),
# which is what would kill it first.
AGGREGATE_BUDGET_S = _budget("AGGREGATE_S", 110)
# Create, run, poll, fetch. Dominated by the executor's polling interval rather
# than by the query.
ASYNC_BUDGET_S = _budget("ASYNC_S", 180)


def _timed(fn):
    started = time.monotonic()
    result = fn()
    return result, time.monotonic() - started


def _rows(response) -> list[dict]:
    return list(csv.DictReader(io.StringIO(response.text)))


def _report(request, label: str, elapsed: float, budget: int) -> None:
    """Print the timing whether it passed or failed.

    A guard that only speaks when it trips tells you nothing about the trend;
    the pod's log is where someone looks after a deployment feels slow.
    """
    print(f"\n[timing] {label}: {elapsed:.2f}s (budget {budget}s)")
    del request


def test_a_keyed_lookup_is_index_served(session, request):
    """WHERE on a primary key. If this is slow, nothing else will be fast."""
    first = sync_query(session, "SELECT TOP 1 obs_id FROM ivoa.obscore")
    assert first.status_code == 200, first.text
    obs_id = _rows(first)[0]["obs_id"]

    response, elapsed = _timed(
        lambda: sync_query(
            session, f"SELECT obs_id FROM ivoa.obscore WHERE obs_id = '{obs_id}'"
        )
    )
    _report(request, "keyed lookup", elapsed, POINT_BUDGET_S)
    assert response.status_code == 200, response.text
    assert _rows(response), "the keyed lookup found nothing"
    assert elapsed < POINT_BUDGET_S, (
        f"a primary-key lookup took {elapsed:.1f}s against a {POINT_BUDGET_S}s budget; "
        "this reads as a missing b-tree rather than a busy cluster"
    )


def test_a_top_n_read_is_not_a_full_scan(session, request):
    """TOP N with no predicate should stop early, not read the table."""
    response, elapsed = _timed(
        lambda: sync_query(session, "SELECT TOP 100 obs_id, s_ra, s_dec FROM ivoa.obscore")
    )
    _report(request, "TOP 100", elapsed, POINT_BUDGET_S)
    assert response.status_code == 200, response.text
    assert elapsed < POINT_BUDGET_S, (
        f"TOP 100 took {elapsed:.1f}s against a {POINT_BUDGET_S}s budget"
    )


def test_a_cone_search_uses_the_footprint_index(session, request):
    """The test the GiST indexes exist for.

    The seeder drops them for its load and rebuilds them afterwards. A rebuild
    that failed leaves every functional test in the suite passing and turns
    this into a sequential scan over every seeded row — a factor, not a few
    percent, which is exactly what this budget is sized to catch.
    """
    response, elapsed = _timed(
        lambda: sync_query(
            session,
            "SELECT TOP 20 obs_id FROM ivoa.obscore "
            "WHERE 1 = CONTAINS(POINT('ICRS', s_ra, s_dec), "
            "CIRCLE('ICRS', 201.365, -43.019, 2.0))",
        )
    )
    _report(request, "cone search", elapsed, CONE_BUDGET_S)
    assert response.status_code == 200, response.text
    assert elapsed < CONE_BUDGET_S, (
        f"a cone search took {elapsed:.1f}s against a {CONE_BUDGET_S}s budget; "
        "the most likely cause is a GiST footprint index that was never rebuilt "
        "after seeding"
    )


def test_a_full_table_aggregate_completes_within_the_sync_timeout(session, request):
    """The expensive one, and correctly so: no index helps a full scan.

    This is the query whose failure mode is a proxy timeout rather than a slow
    database — it dies as "server disconnected without sending a response" when
    something between the client and the service caps the response below the
    time the service is allowed to take. Both the ingress annotations and
    syncTimeoutSeconds exist for it.
    """
    response, elapsed = _timed(
        lambda: sync_query(
            session,
            "SELECT dataproduct_type, COUNT(*) AS n, AVG(t_exptime) AS mean_exptime "
            "FROM ivoa.obscore GROUP BY dataproduct_type",
        )
    )
    _report(request, "full-table aggregate", elapsed, AGGREGATE_BUDGET_S)
    assert response.status_code == 200, (
        f"the aggregate failed after {elapsed:.1f}s: {response.text[:300]}"
    )
    assert _rows(response), "the aggregate returned no rows"
    assert elapsed < AGGREGATE_BUDGET_S, (
        f"the aggregate took {elapsed:.1f}s against a {AGGREGATE_BUDGET_S}s budget"
    )


def test_an_async_round_trip_completes_within_budget(session, tap_url, request):
    """The path a long query is supposed to take, timed end to end.

    Mostly a check that the executor is picking work up: a fleet with no
    executor running leaves jobs QUEUED forever, and nothing in the functional
    suite distinguishes that from a slow one until it times out.
    """

    def run() -> str:
        created = session.post(
            f"{tap_url}/async",
            data={
                "LANG": "ADQL",
                "QUERY": "SELECT TOP 1000 obs_id, s_ra, s_dec FROM ivoa.obscore",
                "RESPONSEFORMAT": "csv",
                "PHASE": "RUN",
            },
            timeout=60,
            allow_redirects=True,
        )
        assert created.status_code in (200, 303), created.text
        job_url = created.url.split("?")[0]
        deadline = time.monotonic() + QUERY_TIMEOUT_S
        phase = ""
        while time.monotonic() < deadline:
            phase = session.get(f"{job_url}/phase", timeout=30).text.strip()
            if phase in ("COMPLETED", "ERROR", "ABORTED"):
                break
            time.sleep(1)
        assert phase == "COMPLETED", f"job ended in {phase or 'no phase'}"
        return job_url

    job_url, elapsed = _timed(run)
    _report(request, "async round trip", elapsed, ASYNC_BUDGET_S)

    results = session.get(f"{job_url}/results/result", timeout=QUERY_TIMEOUT_S)
    assert results.status_code == 200, results.text
    assert len(_rows(results)) == 1000, "the job did not return the rows it was asked for"
    assert elapsed < ASYNC_BUDGET_S, (
        f"an async round trip took {elapsed:.1f}s against a {ASYNC_BUDGET_S}s budget; "
        "check that tap-executor is running and picking up queued jobs"
    )


@pytest.mark.parametrize("fmt", ["csv", "votable", "parquet"])
def test_result_writers_stream_rather_than_buffer(session, request, fmt):
    """Ten thousand rows in three formats, each inside the point budget.

    A writer that buffers the whole result before emitting a byte scales with
    the row count instead of staying flat; at 10,000 rows that is the
    difference between a second and a minute.
    """
    response, elapsed = _timed(
        lambda: sync_query(
            session, "SELECT TOP 10000 obs_id, s_ra, s_dec FROM ivoa.obscore", fmt=fmt
        )
    )
    _report(request, f"10k rows as {fmt}", elapsed, POINT_BUDGET_S)
    assert response.status_code == 200, f"{fmt}: {response.text[:200]}"
    assert response.content, f"{fmt} returned an empty body"
    assert elapsed < POINT_BUDGET_S, (
        f"writing 10,000 rows as {fmt} took {elapsed:.1f}s against a "
        f"{POINT_BUDGET_S}s budget"
    )
