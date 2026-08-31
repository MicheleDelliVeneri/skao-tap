"""Component tests for UWS 1.1 completeness (roadmap package 3): WAIT
blocking requests, AFTER job-list filtering, and real ABORT that cancels
the executing PostgreSQL backend."""

import datetime
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

pytestmark = pytest.mark.component

QUICK_QUERY = "SELECT TOP 3 source_id, source_name FROM ska.continuum_sources"

# The slow query must spend its time in *execution*, where PostgreSQL
# checks for interrupts at every tuple, not in *planning*: a many-relation
# cross join stalls the join-order planner, which honors neither
# pg_cancel_backend nor pg_terminate_backend for tens of seconds (observed
# in CI: a backend shrugging off six cancels and two terminates inside
# DECLARE). A 2-relation cross join over a large seeded table plans in
# microseconds and would take hours to execute — cancellable instantly.
SLOW_QUERY = "SELECT COUNT(*) AS n FROM ska.abort_fodder AS a JOIN ska.abort_fodder AS b ON 1=1"


@pytest.fixture(scope="module")
def abort_fodder(tap_service, database_url):
    """A ~2M-row table registered in TAP_SCHEMA, giving SLOW_QUERY a
    ~4e12-tuple execution with a trivially cheap plan."""
    import psycopg

    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ska.abort_fodder AS"
            " SELECT g AS id FROM generate_series(1, 2000000) AS g"
        )
        conn.execute("GRANT SELECT ON ska.abort_fodder TO tap_reader")
        conn.execute(
            "INSERT INTO tap_schema.tables (schema_name, table_name, table_type, description)"
            " VALUES ('ska', 'ska.abort_fodder', 'table', 'abort-test fodder')"
            " ON CONFLICT (table_name) DO NOTHING"
        )
        conn.execute(
            "INSERT INTO tap_schema.columns (table_name, column_name, datatype)"
            " VALUES ('ska.abort_fodder', 'id', 'long')"
            " ON CONFLICT (table_name, column_name) DO NOTHING"
        )
    return "ska.abort_fodder"


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


def test_committed_abort_wins_while_run_waits_on_the_row(tap_service, database_url):
    """Force RUN to wait behind an uncommitted ABORT on a real row."""
    import psycopg
    from egernia_core import uws

    job_url = _create(tap_service, QUICK_QUERY, run=False)
    job_id = job_url.rsplit("/", 1)[-1]

    def run_job():
        with psycopg.connect(database_url) as run_conn:
            return uws.update_job(
                run_conn,
                job_id,
                expected_phases=("PENDING", "HELD"),
                phase="QUEUED",
                query_sql="SELECT source_id FROM ska.continuum_sources LIMIT 3",
                query_tables=["ska.continuum_sources"],
            )

    with psycopg.connect(database_url) as abort_conn:
        abort_conn.execute(
            "UPDATE uws.jobs SET phase = 'ABORTED' WHERE job_id = %s",
            (job_id,),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending_run = pool.submit(run_job)
            with psycopg.connect(database_url) as observer:
                deadline = time.monotonic() + 10
                waiting = False
                while time.monotonic() < deadline:
                    row = observer.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity"
                        " WHERE wait_event_type = 'Lock'"
                        " AND query LIKE 'UPDATE uws.jobs SET phase = %')"
                    ).fetchone()
                    assert row is not None
                    waiting = row[0]
                    if waiting:
                        break
                    time.sleep(0.05)
                assert waiting, "RUN never reached the locked conditional update"
            abort_conn.commit()
            assert not pending_run.result(timeout=10)

    assert httpx.get(f"{job_url}/phase", timeout=10).text == "ABORTED"


def test_expired_executor_claim_is_recovered(tap_service, database_url):
    """A persisted claim from a dead process is requeued and completed."""
    import psycopg

    job_url = _create(tap_service, QUICK_QUERY, run=False)
    job_id = job_url.rsplit("/", 1)[-1]
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE uws.jobs SET phase = 'EXECUTING', start_time = now() - interval '1 minute',"
            " worker_id = 'dead-worker', lease_expires = now() - interval '1 second',"
            " query_sql = 'SELECT source_id, source_name FROM ska.continuum_sources LIMIT 3',"
            " query_tables = ARRAY['ska.continuum_sources']"
            " WHERE job_id = %s",
            (job_id,),
        )

    assert _wait_final(job_url, timeout_s=30) == "COMPLETED"
    with psycopg.connect(database_url) as conn:
        owner = conn.execute(
            "SELECT worker_id, lease_expires FROM uws.jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    assert owner == (None, None)


def test_abort_cancels_running_query(tap_service, database_url, abort_fodder):
    import psycopg

    job_url = _create(tap_service, SLOW_QUERY)
    # wait until the executor has actually claimed it
    phase = ""
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
        active = []
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


def test_abort_immediately_after_run(tap_service, database_url, abort_fodder):
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
        active = []
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


def test_json_api_wait_after_and_abort(tap_service, abort_fodder):
    api = tap_service.rsplit("/tap", 1)[0] + "/api/v1"
    job = httpx.post(f"{api}/jobs", json={"query": QUICK_QUERY, "run": True}, timeout=30).json()
    deadline = time.monotonic() + 60
    while True:
        done = httpx.get(f"{api}/jobs/{job['job_id']}", params={"wait": 20}, timeout=30).json()
        if done["phase"] in FINAL_PHASES or time.monotonic() > deadline:
            break
    assert done["phase"] == "COMPLETED"

    slow = httpx.post(f"{api}/jobs", json={"query": SLOW_QUERY, "run": True}, timeout=30).json()
    current = {"phase": ""}
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
