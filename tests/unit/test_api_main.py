"""Unit tests for the app wiring in egernia_api.main and the VOSI endpoints,
served over a fake in-memory pool (see conftest)."""

import pytest

QUERY = "SELECT source_id, ra FROM ska.continuum_sources"


def test_root_redirects_to_capabilities(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/tap/capabilities"


def test_availability_reports_available(client):
    response = client.get("/tap/availability")
    assert response.status_code == 200
    assert "<vosi:available>true</vosi:available>" in response.text


def test_capabilities_lists_tap_and_vosi(client):
    response = client.get("/tap/capabilities")
    assert response.status_code == 200
    assert 'standardID="ivo://ivoa.net/std/TAP"' in response.text
    assert "ivo://ivoa.net/std/VOSI#tables" in response.text
    assert "<alias>parquet</alias>" in response.text


def test_tables_renders_tap_schema(client):
    response = client.get("/tap/tables")
    assert response.status_code == 200
    assert "<name>ska.continuum_sources</name>" in response.text
    assert "<unit>deg</unit>" in response.text
    assert "<ucd>pos.eq.ra</ucd>" in response.text
    assert "continuum sources" in response.text


def test_examples_page(client):
    response = client.get("/tap/examples")
    assert response.status_code == 200
    assert 'property="query"' in response.text


def test_sync_get_returns_votable(client):
    response = client.get("/tap/sync", params={"QUERY": QUERY})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-votable+xml")
    assert "<TR><TD>1</TD><TD>62.1</TD></TR>" in response.text


def test_sync_post_form_csv(client):
    response = client.post(
        "/tap/sync", data={"query": QUERY, "responseformat": "csv", "lang": "ADQL"}
    )
    assert response.status_code == 200
    assert response.text.splitlines()[0] == "source_id,ra"
    assert "1,62.1" in response.text


def test_sync_get_capabilities_compat_redirect(client):
    response = client.get(
        "/tap/sync", params={"REQUEST": "getCapabilities"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/capabilities")


def test_sync_missing_query_is_dali_error(client):
    response = client.get("/tap/sync")
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/x-votable+xml")
    assert 'value="ERROR"' in response.text
    assert "missing required parameter QUERY" in response.text


def test_api_errors_are_json(client):
    response = client.post("/api/v1/query", json={"query": "SELECT x FROM private.stuff"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "UsageError"
    assert "not published" in body["message"]


def test_bootstrap_retries_then_succeeds(fake_db, monkeypatch):
    from egernia_api import main
    from egernia_core import bootstrap

    calls = {"n": 0}

    def flaky(conn, plugin):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db not ready")

    monkeypatch.setattr(bootstrap.ingest, "ensure_schema", flaky)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda s: None)
    main._bootstrap_metadata(attempts=3)
    # one failed call on the first attempt, then one call per active plugin
    assert calls["n"] == 1 + len(main.active_plugins())


def test_bootstrap_fails_after_attempts(fake_db, monkeypatch):
    from egernia_api import main
    from egernia_core import bootstrap

    def broken(conn, plugin):
        raise RuntimeError("db never ready")

    monkeypatch.setattr(bootstrap.ingest, "ensure_schema", broken)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="schema bootstrap failed after 2 attempts"):
        main._bootstrap_metadata(attempts=2)


def test_pool_exhaustion_answers_503_with_retry_after(client, monkeypatch):
    """A full pool is a capacity condition, not a fault.

    Before this, waiting for a connection ran to psycopg's 30s default and
    then surfaced as a 500 — a hang followed by the wrong answer, which a
    proxy would not retry.
    """
    from egernia_api.queries import query as query_module
    from psycopg_pool import PoolTimeout

    def exhausted(*args, **kwargs):
        raise PoolTimeout("couldn't get a connection after 5.00 sec")

    monkeypatch.setattr(query_module, "db_connection", exhausted)
    response = client.post(
        "/tap/sync", data={"LANG": "ADQL", "QUERY": "SELECT ra FROM ska.continuum_sources"}
    )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert "connections are busy" in response.text


# -- probes ------------------------------------------------------------------
#
# These exist because /tap/availability was used for both probes: it reports on
# the database, so it queues for a pooled connection, and under load the
# liveness probe's one-second default timeout expired and Kubernetes SIGKILLed
# a busy-but-working API. Measured twice in a benchmark run at an offered rate
# inside the service's own capacity.


def test_liveness_touches_nothing_outside_the_process(client, monkeypatch):
    """A wedged process is what liveness is for, and a restart is the remedy.
    A saturated pool is neither, so this must not consult the database at all —
    verified by making any pool access explode."""
    from egernia_api import main as main_module

    def explode():
        raise AssertionError("liveness must not touch the pool")

    monkeypatch.setattr(main_module, "pool", explode)
    response = client.get("/health/live")
    assert response.status_code == 200


def test_readiness_reports_a_busy_pool_as_still_ready(client, monkeypatch):
    """A full pool is a healthy service under load. Answering "not ready" would
    take a working pod out of the Service and push its share onto pods in
    exactly the same state."""
    from egernia_api import main as main_module
    from psycopg_pool import PoolTimeout

    def timeout() -> None:
        raise PoolTimeout("every connection is in use")

    monkeypatch.setattr(main_module, "_probe_database", timeout)
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert "busy" in response.text


def test_readiness_reports_an_unreachable_database_as_not_ready(client, monkeypatch):
    """The case readiness exists for: this pod cannot serve, so it should stop
    being sent traffic."""
    from egernia_api import main as main_module

    def broken() -> None:
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr(main_module, "_probe_database", broken)
    response = client.get("/health/ready")
    assert response.status_code == 503


def test_readiness_does_not_publish_why_it_failed(client, monkeypatch):
    """This endpoint is reachable without a token by design, and a connection
    error names the host, port and user it could not reach. The kubelet needs
    the status code; nobody needs the topology."""
    from egernia_api import main as main_module

    def broken() -> None:
        raise RuntimeError(
            'connection to server at "db.internal" (10.1.2.3), '
            "port 5432 failed: FATAL: password authentication failed "
            'for user "tap"'
        )

    monkeypatch.setattr(main_module, "_probe_database", broken)
    response = client.get("/health/ready")
    assert response.status_code == 503
    for leaked in ("db.internal", "10.1.2.3", "5432", "tap", "password"):
        assert leaked not in response.text, leaked


def test_availability_is_still_the_vosi_resource(client):
    """The probes moved off it; the standard endpoint has not changed."""
    response = client.get("/tap/availability")
    assert response.status_code == 200
    assert "<vosi:available>true</vosi:available>" in response.text
