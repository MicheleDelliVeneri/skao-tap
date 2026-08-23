"""What a client that arrives without a token is told.

A bare 401 leaves a client guessing which IAM to talk to. These tests pin the
IVOA AuthVO challenge — in the header and in the body, since the SRCNet
reference client reads it from the body — and which paths can still be
reached before a client has any credential at all.
"""

import json

import pytest
from tap_api.auth import needs_token

ROLES = json.dumps({"metadata.delete": {"groups": ["/ska/science-metadata/admin"]}})
QUERY = "SELECT source_id, ra FROM ska.continuum_sources"


@pytest.fixture
def secured(auth_settings, stub_iam, iam_issuer, iam_audience):
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=ROLES,
        iam_issuer=iam_issuer,
        iam_audience=iam_audience,
    )


@pytest.fixture
def bearer(make_token):
    def build(**overrides):
        return {"Authorization": f"Bearer {make_token(**overrides)}"}

    return build


# -- the challenge ----------------------------------------------------------


def test_a_request_with_no_token_is_told_where_the_iam_is(client, secured, iam_issuer):
    response = client.get("/api/v1/software")
    challenge = response.headers["www-authenticate"]
    assert response.status_code == 401
    assert "ivoa_bearer" in challenge
    assert 'error="invalid_request"' in challenge
    assert f'discovery_url="{iam_issuer}/.well-known/openid-configuration"' in challenge


def test_the_challenge_keeps_the_rfc_6750_scheme_too(client, secured):
    """A client that reads only the first challenge must still learn it needs
    a bearer token."""
    challenge = client.get("/api/v1/software").headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert challenge.index("Bearer") < challenge.index("ivoa_bearer")


def test_the_challenge_is_in_the_body_as_well(client, secured):
    """Where the SRCNet reference client (the DM product streamer) reads it."""
    body = client.get("/api/v1/software").json()
    assert "WWW-Authenticate: ivoa_bearer" in body["message"]
    assert 'discovery_url="' in body["message"]


def test_a_presented_but_invalid_token_is_a_different_error_code(client, secured, bearer):
    """invalid_token, not invalid_request: the client had a credential, so
    re-running discovery is not the fix — getting a fresh token is."""
    import time

    response = client.get("/api/v1/software", headers=bearer(exp=int(time.time()) - 10))
    challenge = response.headers["www-authenticate"]
    assert response.status_code == 401
    assert 'error="invalid_token"' in challenge
    assert "expired" in response.json()["message"]


def test_an_explicit_well_known_url_wins(client, secured, auth_settings):
    """An IAM whose discovery document is not at the conventional path."""
    auth_settings(iam_well_known_url="https://iam.example.org/oidc/config")
    challenge = client.get("/api/v1/software").headers["www-authenticate"]
    assert 'discovery_url="https://iam.example.org/oidc/config"' in challenge


def test_the_vo_error_document_carries_the_challenge_too(
    client, auth_settings, stub_iam, iam_issuer, iam_audience, fake_db
):
    """The TAP endpoints answer DALI VOTables, not JSON — the challenge has to
    survive that rendering too. Reached by gating the query surface, the one
    way a TAP path answers 401."""
    oper = "/ska/science-metadata/oper"
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=json.dumps(
            {
                "jobs.create": {"groups": [oper]},
                "jobs.mutate": {"groups": [oper]},
                "jobs.delete": {"groups": [oper]},
                "query.sync": {"groups": [oper]},
            }
        ),
        auth_gated_operations="jobs.create,jobs.mutate,jobs.delete,query.sync",
        iam_issuer=iam_issuer,
        iam_audience=iam_audience,
    )
    response = client.post("/tap/sync", data={"LANG": "ADQL", "QUERY": QUERY})
    assert response.status_code == 401
    assert "ivoa_bearer" in response.headers["www-authenticate"]
    assert response.headers["content-type"].startswith("application/x-votable")
    assert "ivoa_bearer" in response.text


# -- what stays reachable without one ---------------------------------------


def test_a_malformed_authorization_header_is_the_discovery_case(client, secured):
    """No usable credential was presented, so invalid_token would tell the
    client to refresh a token it never had."""
    for header in ("Basic abc", "Bearer", "Bearer   "):
        response = client.get("/api/v1/software", headers={"Authorization": header})
        challenge = response.headers["www-authenticate"]
        assert response.status_code == 401, header
        assert 'error="invalid_request"' in challenge, header
        assert "discovery_url=" in challenge, header


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/tap/availability",
        "/tap/capabilities",
        "/tap/tables",
        "/tap/examples",
        "/api/v1/auth",
        "/openapi.json",
        "/docs",
    ],
)
def test_discovery_and_health_stay_open(client, secured, path):
    """A liveness probe, a registry harvester and a client looking for the IAM
    cannot hold a token; gating these would make the service unmonitorable,
    unregisterable and undiscoverable."""
    assert client.get(path).status_code == 200


def test_reading_metadata_through_tap_needs_a_token_by_default(client, secured, fake_db):
    """A VO client cannot authenticate, so opening this is a decision a
    deployment takes rather than inherits."""
    synchronous = client.post("/tap/sync", data={"LANG": "ADQL", "QUERY": QUERY})
    job = client.post("/tap/async", data={"LANG": "ADQL", "QUERY": QUERY}, follow_redirects=False)
    assert synchronous.status_code == 401
    assert job.status_code == 401
    assert "ivoa_bearer" in synchronous.headers["www-authenticate"]


def test_reading_metadata_through_tap_is_open_when_a_deployment_says_so(
    client, secured, auth_settings, fake_db
):
    auth_settings(auth_anonymous_queries=True)
    synchronous = client.post("/tap/sync", data={"LANG": "ADQL", "QUERY": QUERY})
    job = client.post("/tap/async", data={"LANG": "ADQL", "QUERY": QUERY}, follow_redirects=False)
    assert synchronous.status_code == 200
    assert job.status_code == 303


def test_a_job_subresource_is_open_like_the_job(client, secured, auth_settings, fake_db):
    """A job is a tree, and every branch of it belongs to the same read."""
    auth_settings(auth_anonymous_queries=True)
    created = client.post(
        "/tap/async", data={"LANG": "ADQL", "QUERY": QUERY}, follow_redirects=False
    )
    job_id = created.headers["location"].rsplit("/", 1)[-1]
    assert client.get(f"/tap/async/{job_id}/phase").status_code == 200
    assert client.get(f"/tap/async/{job_id}/parameters").status_code == 200


def test_the_json_query_facade_is_not_opened_by_the_switch(client, secured, auth_settings):
    """The switch exists for clients that cannot authenticate. A JSON client
    can, so it is out of scope even when TAP is open."""
    auth_settings(auth_anonymous_queries=True)
    assert client.post("/api/v1/query", json={"query": QUERY}).status_code == 401
    assert client.post("/api/v1/jobs", json={"query": QUERY}).status_code == 401


def test_the_auth_endpoint_advertises_the_discovery_url(client, secured, iam_issuer):
    """So a client can find the IAM without having to provoke a 401 first."""
    body = client.get("/api/v1/auth").json()
    assert body["token_required"] is True
    assert body["anonymous_queries"] is False
    assert body["discovery_url"] == f"{iam_issuer}/.well-known/openid-configuration"


def test_tap_1_0_capability_discovery_is_not_a_query(client, secured):
    """GET /tap/sync?REQUEST=getCapabilities redirects to /capabilities, which
    is open — so demanding a token for the redirect breaks older clients while
    protecting nothing."""
    response = client.get(
        "/tap/sync", params={"REQUEST": "getCapabilities"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/capabilities")


def test_capability_discovery_survives_the_query_gate_too(
    client, auth_settings, stub_iam, iam_issuer, iam_audience
):
    """Not just the token requirement: a site that gates the query surface
    must not lose capability discovery either."""
    oper = "/ska/science-metadata/oper"
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=json.dumps(
            {
                "jobs.create": {"groups": [oper]},
                "jobs.mutate": {"groups": [oper]},
                "jobs.delete": {"groups": [oper]},
                "query.sync": {"groups": [oper]},
            }
        ),
        auth_gated_operations="jobs.create,jobs.mutate,jobs.delete,query.sync",
        iam_issuer=iam_issuer,
        iam_audience=iam_audience,
    )
    response = client.get(
        "/tap/sync", params={"REQUEST": "getCapabilities"}, follow_redirects=False
    )
    assert response.status_code == 303


def test_a_real_query_is_still_gated(client, secured, fake_db):
    """The exemption is for the discovery form only."""
    assert client.get("/tap/sync", params={"QUERY": "SELECT 1", "LANG": "ADQL"}).status_code == 401


@pytest.mark.parametrize(
    ("label", "url"),
    [
        # the handler compares REQUEST as-is, so a mis-cased value is a query
        # to it; an exemption that lowercased would have run it without a token
        ("mis-cased REQUEST", "/tap/sync?REQUEST=GETCAPABILITIES&LANG=ADQL&QUERY=SELECT+1"),
        # gather_params keeps the last value, so this is doQuery to the handler
        (
            "duplicate REQUEST, query last",
            "/tap/sync?REQUEST=getCapabilities&REQUEST=doQuery&LANG=ADQL&QUERY=SELECT+1",
        ),
    ],
)
def test_the_discovery_exemption_cannot_be_widened_into_a_query(
    client, secured, fake_db, label, url
):
    """The exemption must never be wider than the redirect it exists for: a
    request the handler runs as a query must not reach it as discovery."""
    response = client.get(url, follow_redirects=False)
    assert response.status_code == 401, f"{label} bypassed the token requirement"


def test_a_post_form_cannot_ride_in_on_a_discovery_query_string(client, secured, fake_db):
    """gather_params merges the form *over* the query string, so on a POST the
    query string does not decide what the handler does — this would otherwise
    be exempted as discovery and then run as a query."""
    response = client.post(
        "/tap/sync?REQUEST=getCapabilities",
        data={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": "SELECT 1"},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_discovery_by_post_needs_a_token(client, secured, fake_db):
    """A narrowing worth writing down: the exemption is the GET form only,
    because reading a POST body here would consume it before the handler."""
    response = client.post("/tap/sync", data={"REQUEST": "getCapabilities"}, follow_redirects=False)
    assert response.status_code == 401


def test_the_last_request_value_decides_like_the_handler(client, secured, fake_db):
    """Discovery last: the handler redirects, so the exemption applies too."""
    response = client.get(
        "/tap/sync?REQUEST=doQuery&REQUEST=getCapabilities", follow_redirects=False
    )
    assert response.status_code == 303


# -- the setting ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "required"),
    [
        ("/tap/sync", True),
        ("/tap/async", True),
        ("/tap/async/abc/results/result", True),
        ("/tap/capabilities", False),
        ("/tap/capabilities/", False),
        ("/api/v1/auth", False),
        ("/api/v1/software", True),
        ("/api/v1/query", True),
        ("/api/v1/jobs", True),
        ("/api/v1/tables", True),
    ],
)
def test_which_paths_need_a_token(path, required):
    assert needs_token(path) is required


@pytest.mark.parametrize(
    ("path", "required"),
    [
        ("/tap/sync", False),
        ("/tap/sync/", False),
        ("/tap/async", False),
        ("/tap/async/abc/results/result", False),
        # not a prefix match on a different route that merely starts the same
        ("/tap/synchronised", True),
        ("/api/v1/query", True),
        ("/api/v1/software", True),
    ],
)
def test_which_paths_need_a_token_with_anonymous_queries(auth_settings, path, required):
    auth_settings(auth_anonymous_queries=True)
    assert needs_token(path) is required


def test_the_registry_record_is_not_gated(client, secured):
    """404 because this deployment publishes no record, not 401: a harvester
    cannot hold a token, so the endpoint must not ask for one."""
    assert client.get("/tap/registry").status_code == 404


@pytest.mark.parametrize("path", ["/docs/oauth2-redirect", "/redoc"])
def test_the_doc_routes_need_no_token(path):
    """Asserted against the predicate: FastAPI serves these outside the app
    dependency, so a regression in the list would not show up over HTTP."""
    assert needs_token(path) is False


def test_nothing_needs_a_token_when_the_requirement_is_off(auth_settings):
    auth_settings(auth_require_token=False)
    assert needs_token("/api/v1/software") is False


def test_turning_the_requirement_off_restores_anonymous_reads(
    client, auth_settings, stub_iam, iam_issuer, iam_audience
):
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=ROLES,
        auth_require_token=False,
        iam_issuer=iam_issuer,
        iam_audience=iam_audience,
    )
    assert client.get("/api/v1/software").status_code == 200


def test_authentication_disabled_leaves_everything_open(client):
    assert client.get("/api/v1/software").status_code == 200
    assert client.get("/api/v1/tables").status_code == 200
