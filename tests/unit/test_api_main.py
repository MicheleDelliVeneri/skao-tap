"""Unit tests for the app wiring in tap_api.main and the VOSI endpoints,
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
    from tap_api import main

    calls = {"n": 0}

    def flaky(conn, plugin):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db not ready")

    monkeypatch.setattr(main.ingest, "ensure_schema", flaky)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    main._bootstrap_metadata(attempts=3)
    # one failed call on the first attempt, then one call per active plugin
    assert calls["n"] == 1 + len(main.active_plugins())


def test_bootstrap_fails_after_attempts(fake_db, monkeypatch):
    from tap_api import main

    def broken(conn, plugin):
        raise RuntimeError("db never ready")

    monkeypatch.setattr(main.ingest, "ensure_schema", broken)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="metadata bootstrap failed after 2 attempts"):
        main._bootstrap_metadata(attempts=2)
