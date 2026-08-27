"""Correlation ids, SQL tagging and the metrics endpoint.

What these pin is the ability to answer "which request caused this?" without
reproducing anything locally — the thing whose absence made the last three
performance fixes take a local harness and a sampling profiler.
"""

import time

import pytest
from egernia_core import observability as obs
from prometheus_client import generate_latest

QUERY = "SELECT source_id, ra FROM ska.continuum_sources"


# -- the correlation id -----------------------------------------------------


def test_a_request_gets_an_id_and_is_told_what_it_is(client):
    response = client.get("/tap/availability")
    assert response.headers[obs.REQUEST_ID_HEADER]


def test_an_id_supplied_by_the_caller_is_kept(client):
    """A gateway or client that already traces a request must not have that
    broken here."""
    response = client.get("/tap/availability", headers={obs.REQUEST_ID_HEADER: "caller-chosen-id"})
    assert response.headers[obs.REQUEST_ID_HEADER] == "caller-chosen-id"


def test_ids_differ_between_requests(client):
    first = client.get("/tap/availability").headers[obs.REQUEST_ID_HEADER]
    second = client.get("/tap/availability").headers[obs.REQUEST_ID_HEADER]
    assert first != second


def test_every_response_names_the_pod_that_served_it(client):
    """X-Served-By carries the hostname (the pod name in Kubernetes), so
    "which replica answered" is stated by the response rather than inferred
    from a proxy — an inference that on a deep queue measures the queue."""
    import socket

    response = client.get("/tap/availability")
    assert response.headers["X-Served-By"] == socket.gethostname()


def test_the_job_records_the_request_that_created_it(client, fake_db):
    """This is what lets an executor's records name the request: the id is on
    the row, not only in the API's own logs."""
    created = client.post(
        "/tap/async",
        data={"LANG": "ADQL", "QUERY": QUERY},
        headers={obs.REQUEST_ID_HEADER: "job-origin-id"},
        follow_redirects=False,
    )
    job_id = created.headers["location"].rsplit("/", 1)[-1]
    assert fake_db.jobs[job_id]["request_id"] == "job-origin-id"


# -- tagging the SQL --------------------------------------------------------


@pytest.fixture
def with_request_id():
    with obs.request_context("abc123"):
        yield "abc123"


def test_a_statement_is_tagged_with_the_request(with_request_id):
    assert obs.tag_sql("SELECT 1") == "SELECT 1 /* rid=abc123 */"


def test_the_tag_goes_inside_the_statement(with_request_id):
    """After the semicolon it would be a second, empty statement."""
    assert obs.tag_sql("SELECT 1;") == "SELECT 1 /* rid=abc123 */;"


def test_a_tagged_statement_still_starts_with_its_verb(with_request_id):
    """Prefixing would break everything that reads the start of a query."""
    assert obs.tag_sql("SELECT 1").startswith("SELECT")


def test_without_an_id_the_statement_is_untouched():
    with obs.request_context(None):
        assert obs.tag_sql("SELECT 1;") == "SELECT 1;"


def test_the_query_carries_the_tag_to_the_database(client, fake_db):
    client.post(
        "/tap/sync",
        data={"LANG": "ADQL", "QUERY": QUERY},
        headers={obs.REQUEST_ID_HEADER: "deadbeef"},
    )
    executed = [s for s in fake_db.statements if "continuum_sources" in s]
    assert executed, "the query never reached the database"
    assert any("/* rid=deadbeef */" in s for s in executed)


# -- the metrics endpoint ---------------------------------------------------


def test_metrics_are_served_without_a_redirect(client):
    """Scrape configs and pod annotations say /metrics; a scraper that does
    not follow redirects has to get the exposition from that exact path."""
    response = client.get("/metrics", follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize(
    "metric",
    [
        "tap_db_pool_wait_seconds",  # the signal the pool collapse needed
        "tap_db_pool_exhausted_total",
        "tap_db_connections_in_use",
        "tap_query_duration_seconds",
        "tap_jobs",
        "tap_oldest_queued_job_seconds",
        "tap_jobs_completed_total",
    ],
)
def test_the_endpoint_exposes_the_metric(client, metric):
    assert metric in client.get("/metrics").text


def test_running_a_query_records_its_duration(client, fake_db):
    before = generate_latest(obs.REGISTRY).decode()
    client.post("/tap/sync", data={"LANG": "ADQL", "QUERY": QUERY})
    after = generate_latest(obs.REGISTRY).decode()

    def sync_count(text):
        for line in text.splitlines():
            if line.startswith('tap_query_duration_seconds_count{kind="sync"}'):
                return float(line.split()[-1])
        return 0.0

    assert sync_count(after) == sync_count(before) + 1


def test_the_pool_wait_is_recorded_even_when_it_is_short(client, fake_db):
    """The histogram has to see the healthy case too, or its tail means
    nothing."""
    before = generate_latest(obs.REGISTRY).decode()
    client.get("/tap/tables")
    after = generate_latest(obs.REGISTRY).decode()

    def wait_count(text):
        for line in text.splitlines():
            if line.startswith("tap_db_pool_wait_seconds_count"):
                return float(line.split()[-1])
        return 0.0

    assert wait_count(after) > wait_count(before)


# -- a caller's id is not trusted -------------------------------------------


@pytest.mark.parametrize(
    ("hostile", "why"),
    [
        ("*/ DROP TABLE uws.jobs; --", "closes the SQL comment"),
        ("id */ UNION SELECT 1 /*", "reopens it around injected SQL"),
        ("id\r\nX-Injected: yes", "splits the response header"),
        ("id\nfake log line", "forges a log record"),
        ("a" * 129, "unbounded length"),
    ],
)
def test_a_hostile_request_id_is_replaced_not_escaped(client, hostile, why):
    """The id reaches a SQL comment, a response header and the logs, so it is
    refused rather than quoted."""
    response = client.get("/tap/availability", headers={obs.REQUEST_ID_HEADER: hostile})
    returned = response.headers[obs.REQUEST_ID_HEADER]
    assert returned != hostile, why
    assert obs.SAFE_REQUEST_ID.fullmatch(returned)


def test_tagging_refuses_an_unsafe_id_even_if_something_set_one(fake_db):
    """Defence in depth: the check does not rely on the middleware having run."""
    with obs.request_context("*/ DROP TABLE x; --"):
        assert obs.tag_sql("SELECT 1") == "SELECT 1"


def test_a_hostile_id_never_reaches_the_database(client, fake_db):
    client.post(
        "/tap/sync",
        data={"LANG": "ADQL", "QUERY": QUERY},
        headers={obs.REQUEST_ID_HEADER: "*/ DROP TABLE uws.jobs; --"},
    )
    assert not any("DROP TABLE" in s for s in fake_db.statements)


def test_the_sync_duration_is_recorded_when_the_stream_ends():
    """It says "to the last row", so it must observe at the end and not after
    the first chunk. Tested on the wrapper directly: through the test client
    the whole body is read before the call returns, so it could not tell the
    two apart."""
    from egernia_api.queries.query import _timed

    def sum_of():
        for line in generate_latest(obs.REGISTRY).decode().splitlines():
            if line.startswith('tap_query_duration_seconds_sum{kind="sync"}'):
                return float(line.split()[-1])
        return 0.0

    before = sum_of()
    stream = _timed(iter([b"a", b"b"]), time.perf_counter())
    next(stream)
    assert sum_of() == before, "nothing is recorded while rows are still coming"
    list(stream)  # exhaust
    assert sum_of() > before


def test_an_abandoned_stream_is_still_measured():
    """A client that disconnects halfway is a slow query too — dropping it
    would bias the metric towards the requests that finished."""
    from egernia_api.queries.query import _timed

    def count():
        for line in generate_latest(obs.REGISTRY).decode().splitlines():
            if line.startswith('tap_query_duration_seconds_count{kind="sync"}'):
                return float(line.split()[-1])
        return 0.0

    before = count()
    stream = _timed(iter([b"a", b"b", b"c"]), time.perf_counter())
    next(stream)
    stream.close()  # the client went away
    assert count() == before + 1


def test_the_pool_wait_is_only_the_wait(fake_db):
    """It is the backpressure signal, so held time must stay out of it: a sync
    query keeps its connection for the whole of the client's download, and
    counting that as waiting would make a busy pool and a big result look the
    same."""
    from egernia_core import db

    before = _metric("tap_db_pool_wait_seconds_sum")
    with db.connection():
        time.sleep(0.2)
    assert _metric("tap_db_pool_wait_seconds_sum") - before < 0.05


def test_a_wait_that_ends_in_a_timeout_is_still_recorded(monkeypatch, fake_db):
    """The longest wait there is must not be the one that goes unmeasured."""
    from egernia_core import db
    from psycopg_pool import PoolTimeout

    class Exhausted:
        def connection(self):
            return self

        def __enter__(self):
            raise PoolTimeout("no connection is available")

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(db, "pool", Exhausted)
    before = _metric("tap_db_pool_wait_seconds_count")
    with pytest.raises(PoolTimeout), db.connection():
        pass
    assert _metric("tap_db_pool_wait_seconds_count") == before + 1


def test_a_failed_job_is_counted_as_a_failure(monkeypatch, fake_db):
    """Undercounting failures is worse than not counting them: an alert on
    this metric would have stayed quiet."""
    from egernia_executor import worker

    def explode(*args, **kwargs):
        raise RuntimeError("query blew up")

    job = fake_db.add_job(phase="EXECUTING", query_sql="SELECT 1", parameters={})
    monkeypatch.setattr(worker, "run_query", explode, raising=False)

    before = generate_latest(obs.REGISTRY).decode()
    worker.execute_job(job)
    after = generate_latest(obs.REGISTRY).decode()

    def errors(text):
        for line in text.splitlines():
            if line.startswith('tap_jobs_completed_total{phase="ERROR"}'):
                return float(line.split()[-1])
        return 0.0

    assert errors(after) == errors(before) + 1


def _metric(name: str) -> float:
    for line in generate_latest(obs.REGISTRY).decode().splitlines():
        if line.startswith(name):
            return float(line.split()[-1])
    return 0.0


def _run_a_job_that_is_finalized_mid_stream(fake_db, monkeypatch):
    """A job whose phase flips to ABORTED after the query started."""
    from egernia_executor import worker

    job = fake_db.add_job(
        phase="QUEUED",
        parameters={"QUERY": QUERY, "RESPONSEFORMAT": "csv"},
        query_sql=QUERY,
    )
    claimed = worker.claim_job()
    real_stream = worker.result_stream

    def abort_then_stream(*args, **kwargs):
        fake_db.jobs[job["job_id"]]["phase"] = "ABORTED"
        return real_stream(*args, **kwargs)

    monkeypatch.setattr(worker, "result_stream", abort_then_stream)
    worker.execute_job(claimed)


def test_a_job_finalized_while_it_ran_is_still_counted(fake_db, results_dir, monkeypatch):
    """The work happened and the job reached a final phase; discarding the
    result is not a reason to discard the outcome."""
    before = _metric('tap_jobs_completed_total{phase="ABORTED"}')
    _run_a_job_that_is_finalized_mid_stream(fake_db, monkeypatch)
    assert _metric('tap_jobs_completed_total{phase="ABORTED"}') == before + 1


def test_a_job_finalized_while_it_ran_is_still_timed(fake_db, results_dir, monkeypatch):
    """It ran a query to the end, so it belongs in the duration histogram —
    the same argument as the sync stream a client abandoned."""
    before = _metric('tap_query_duration_seconds_count{kind="async"}')
    _run_a_job_that_is_finalized_mid_stream(fake_db, monkeypatch)
    assert _metric('tap_query_duration_seconds_count{kind="async"}') == before + 1


def test_an_abort_before_the_query_starts_is_counted_but_not_timed(
    fake_db, results_dir, monkeypatch
):
    """No query ran, so timing it would pull the low buckets down with work
    that never happened — but the outcome is still an outcome."""
    from egernia_executor import worker

    job = fake_db.add_job(
        phase="QUEUED",
        parameters={"QUERY": QUERY, "RESPONSEFORMAT": "csv"},
        query_sql=QUERY,
    )
    claimed = worker.claim_job()
    fake_db.jobs[job["job_id"]]["phase"] = "ABORTED"  # before the PID is published

    counted = _metric('tap_jobs_completed_total{phase="ABORTED"}')
    timed = _metric('tap_query_duration_seconds_count{kind="async"}')
    worker.execute_job(claimed)
    assert _metric('tap_jobs_completed_total{phase="ABORTED"}') == counted + 1
    assert _metric('tap_query_duration_seconds_count{kind="async"}') == timed


def test_a_deleted_job_is_not_counted_as_an_outcome(fake_db, results_dir, monkeypatch):
    """A row that is gone has no final phase to report; inventing one would put
    a wrong outcome in the metric rather than a missing one."""
    from egernia_executor import worker

    job = fake_db.add_job(
        phase="QUEUED",
        parameters={"QUERY": QUERY, "RESPONSEFORMAT": "csv"},
        query_sql=QUERY,
    )
    claimed = worker.claim_job()
    real_stream = worker.result_stream

    def delete_then_stream(*args, **kwargs):
        del fake_db.jobs[job["job_id"]]
        return real_stream(*args, **kwargs)

    monkeypatch.setattr(worker, "result_stream", delete_then_stream)

    before = generate_latest(obs.REGISTRY).decode()
    worker.execute_job(claimed)
    after = generate_latest(obs.REGISTRY).decode()

    def completions(text):
        return [line for line in text.splitlines() if line.startswith("tap_jobs_completed_total")]

    assert completions(after) == completions(before)


def test_the_request_id_does_not_outlive_the_request(client):
    """Work after the response must not be attributed to it."""
    client.get("/tap/availability", headers={obs.REQUEST_ID_HEADER: "scoped-id"})
    assert obs.request_id() is None


def test_pool_wait_buckets_bracket_the_timeout(auth_settings):
    """A timed-out acquire waits fractionally longer than the timeout; the
    edge pair (t, 1.2t] pins it there instead of letting quantile
    interpolation read the middle of a wide bucket (a 5 s timeout was
    reported as a 9.7 s p95)."""
    from egernia_core.observability import _pool_wait_buckets

    buckets = _pool_wait_buckets()
    assert buckets == tuple(sorted(buckets))
    timeout = 5.0  # the default
    assert timeout in buckets
    assert round(timeout * 1.2, 3) in buckets

    auth_settings(db_pool_timeout_s=2.5)
    rederived = _pool_wait_buckets()
    assert 2.5 in rederived and 3.0 in rederived
