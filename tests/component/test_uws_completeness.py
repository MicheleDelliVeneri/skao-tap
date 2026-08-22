"""Component tests for UWS 1.1 completeness (roadmap package 3): WAIT
blocking requests, AFTER job-list filtering, and real ABORT that cancels
the executing PostgreSQL backend."""

import datetime
import time

import httpx
import pytest

pytestmark = pytest.mark.component

QUICK_QUERY = "SELECT TOP 3 source_id, source_name FROM ska.continuum_sources"

# ~8^10 = 1e9 row combinations to count: CPU-bound for well over a minute,
# produces no result rows until done — safe to abort mid-flight. Explicit
# JOIN ... ON 1=1 keeps the syntactic join order (join_collapse_limit), so
# planning is instant and the time is spent in the (cancellable) executor —
# an implicit 10-way cross join stalls the *planner*, which ignores
# pg_cancel_backend for minutes.
SLOW_QUERY = "SELECT COUNT(*) AS n FROM ska.continuum_sources AS t0 " + " ".join(
    f"JOIN ska.continuum_sources AS t{i} ON 1=1" for i in range(1, 10)
)


FINAL_PHASES = ("COMPLETED", "ERROR", "ABORTED")


def _wait_final(job_url: str, timeout_s: int = 60) -> str:
    """Loop WAIT until a final phase: per UWS 1.1 a WAIT request returns on
    any phase *change* (e.g. QUEUED -> EXECUTING), so clients iterate."""
    deadline = time.monotonic() + timeout_s
    while True:
        phase = httpx.get(f"{job_url}/phase", params={"WAIT": "20"}, timeout=30).text
        if phase in FINAL_PHASES or time.monotonic() > deadline:
            return phase


def _create(tap_service, query, run=True):
    data = {"QUERY": query, "LANG": "ADQL"}
    if run:
        data["PHASE"] = "RUN"
    response = httpx.post(f"{tap_service}/async", data=data, timeout=30, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"]


def test_wait_blocks_until_completion(tap_service):
    job_url = _create(tap_service, QUICK_QUERY)
    assert _wait_final(job_url) == "COMPLETED"
    # a further WAIT on the final phase returns immediately
    started = time.monotonic()
    assert httpx.get(f"{job_url}/phase", params={"WAIT": "20"}, timeout=30).text == "COMPLETED"
    assert time.monotonic() - started < 5


def test_wait_on_job_summary_with_phase_parameter(tap_service):
    job_url = _create(tap_service, QUICK_QUERY, run=False)  # stays PENDING
    started = time.monotonic()
    # client believes QUEUED; actual phase is PENDING -> immediate return
    response = httpx.get(job_url, params={"WAIT": "15", "PHASE": "QUEUED"}, timeout=30)
    assert time.monotonic() - started < 5
    assert "<uws:phase>PENDING</uws:phase>" in response.text
    assert httpx.get(job_url, params={"WAIT": "oops"}, timeout=10).status_code == 400


def test_after_filters_job_list(tap_service):
    first = _create(tap_service, QUICK_QUERY, run=False).rsplit("/", 1)[-1]
    time.sleep(1.1)
    cut = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    time.sleep(0.1)
    second = _create(tap_service, QUICK_QUERY, run=False).rsplit("/", 1)[-1]
    listing = httpx.get(f"{tap_service}/async", params={"AFTER": cut}, timeout=10).text
    assert second in listing
    assert first not in listing
    assert (
        httpx.get(f"{tap_service}/async", params={"AFTER": "not-a-time"}, timeout=10).status_code
        == 400
    )


def test_abort_cancels_running_query(tap_service, database_url):
    import psycopg

    job_url = _create(tap_service, SLOW_QUERY)
    # wait until the executor has actually claimed it
    for _ in range(40):
        phase = httpx.get(f"{job_url}/phase", timeout=10).text
        if phase == "EXECUTING":
            break
        assert phase in ("PENDING", "QUEUED", "EXECUTING"), phase
        time.sleep(0.5)
    assert phase == "EXECUTING"
    time.sleep(1.0)  # let the backend PID be registered and the scan start

    aborted_at = time.monotonic()
    response = httpx.post(
        f"{job_url}/phase", data={"PHASE": "ABORT"}, timeout=10, follow_redirects=False
    )
    assert response.status_code == 303

    phase = _wait_final(job_url, timeout_s=10)
    assert phase == "ABORTED"
    assert time.monotonic() - aborted_at < 15  # cancelled, not run to completion

    # the backend really stopped: no active statement still counting
    with psycopg.connect(database_url) as conn:
        for _ in range(30):
            active = conn.execute(
                "SELECT pid, state, wait_event_type, wait_event,"
                " now() - query_start AS running_for, left(query, 60)"
                " FROM pg_stat_activity"
                " WHERE state = 'active' AND query ILIKE '%%tap_job_%%'"
                " AND pid <> pg_backend_pid()"  # not this introspection query
            ).fetchall()
            if not active:
                break
            time.sleep(0.5)
        assert not active, f"backend still executing after abort: {active}"

    # the abort sticks: the executor must not flip it to COMPLETED or ERROR
    time.sleep(3)
    summary = httpx.get(job_url, timeout=10)
    assert "<uws:phase>ABORTED</uws:phase>" in summary.text
    assert "errorSummary" not in summary.text
    result = httpx.get(f"{job_url}/results/result", timeout=10)
    assert result.status_code == 404


def test_abort_immediately_after_run(tap_service, database_url):
    """Abort racing the executor's PID publication: even if the cancel from
    the API has nothing to hit yet, the executor must notice ABORTED and
    never run the scan to completion."""
    import psycopg

    job_url = _create(tap_service, SLOW_QUERY)  # PHASE=RUN, abort right away
    response = httpx.post(
        f"{job_url}/phase", data={"PHASE": "ABORT"}, timeout=10, follow_redirects=False
    )
    assert response.status_code == 303
    assert httpx.get(f"{job_url}/phase", timeout=10).text == "ABORTED"

    with psycopg.connect(database_url) as conn:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            active = conn.execute(
                "SELECT pid FROM pg_stat_activity WHERE state = 'active'"
                " AND query ILIKE '%%tap_job_%%' AND pid <> pg_backend_pid()"
            ).fetchall()
            if not active:
                break
            time.sleep(0.5)
        assert not active, f"backend still executing after immediate abort: {active}"

    time.sleep(2)
    summary = httpx.get(job_url, timeout=10)
    assert "<uws:phase>ABORTED</uws:phase>" in summary.text
    assert httpx.get(f"{job_url}/results/result", timeout=10).status_code == 404


def test_json_api_wait_after_and_abort(tap_service):
    api = tap_service.rsplit("/tap", 1)[0] + "/api/v1"
    job = httpx.post(f"{api}/jobs", json={"query": QUICK_QUERY, "run": True}, timeout=30).json()
    deadline = time.monotonic() + 60
    while True:
        done = httpx.get(f"{api}/jobs/{job['job_id']}", params={"wait": 20}, timeout=30).json()
        if done["phase"] in FINAL_PHASES or time.monotonic() > deadline:
            break
    assert done["phase"] == "COMPLETED"

    slow = httpx.post(f"{api}/jobs", json={"query": SLOW_QUERY, "run": True}, timeout=30).json()
    for _ in range(40):
        current = httpx.get(f"{api}/jobs/{slow['job_id']}", timeout=10).json()
        if current["phase"] == "EXECUTING":
            break
        time.sleep(0.5)
    assert current["phase"] == "EXECUTING"
    time.sleep(1.0)
    aborted = httpx.post(
        f"{api}/jobs/{slow['job_id']}/phase", json={"phase": "ABORT"}, timeout=10
    ).json()
    assert aborted["phase"] == "ABORTED"

    cut = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    listing = httpx.get(
        f"{api}/jobs", params={"after": cut.strftime("%Y-%m-%dT%H:%M:%SZ")}, timeout=10
    ).json()
    assert any(j["job_id"] == slow["job_id"] for j in listing["jobs"])
