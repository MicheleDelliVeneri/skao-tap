"""HTTP-level tests for the gate: which endpoints demand a token when auth is
enabled, what they answer without one, and that querying stays anonymous."""

import json

import pytest

ROLES = json.dumps(
    {
        "metadata.ingest": {"groups": ["/ska/science-metadata/oper"]},
        "metadata.amend": {"groups": ["/ska/science-metadata/oper"]},
        "metadata.delete": {"groups": ["/ska/science-metadata/admin"]},
    }
)
# what an operator gets by enabling auth and forgetting to write a policy
EMPTY_ROLES = json.dumps({"metadata.ingest": {}, "metadata.amend": {}, "metadata.delete": {}})


@pytest.fixture
def secured(auth_settings, stub_iam, iam_issuer, iam_audience):
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=ROLES,
        iam_issuer=iam_issuer,
        iam_audience=iam_audience,
    )
    return auth_settings


@pytest.fixture
def bearer(make_token):
    """An Authorization header for a token signed by the stub IAM."""

    def build(private=None, **overrides):
        return {"Authorization": f"Bearer {make_token(private, **overrides)}"}

    return build


def test_ingest_without_a_token_is_401_with_a_challenge(client, secured, software_payload):
    response = client.post("/api/v1/software", json=software_payload)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.json()["error"] == "AuthenticationError"


def test_ingest_with_the_required_group_succeeds(client, secured, software_payload, bearer):
    response = client.post("/api/v1/software", json=software_payload, headers=bearer())
    assert response.status_code == 201, response.text


def test_ingest_without_the_required_group_is_403(client, secured, software_payload, bearer):
    headers = bearer(groups=["/ska/science-metadata/user"])
    response = client.post("/api/v1/software", json=software_payload, headers=headers)
    assert response.status_code == 403
    assert response.json()["error"] == "AuthorizationError"


def test_delete_needs_its_own_role(client, secured, software_payload, bearer):
    """Deletion is configured separately from ingest: the ingest role is not enough."""
    ingest_only = bearer(groups=["/ska/science-metadata/oper"])
    client.post("/api/v1/software", json=software_payload, headers=ingest_only)
    url = f"/api/v1/software/{software_payload['uri']}"

    denied = client.delete(url, headers=ingest_only)
    allowed = client.delete(url, headers=bearer(groups=["/ska/science-metadata/admin"]))
    assert denied.status_code == 403
    assert allowed.status_code == 200


def test_amend_is_gated(client, secured, software_payload, bearer):
    oper = bearer(groups=["/ska/science-metadata/oper"])
    client.post("/api/v1/software", json=software_payload, headers=oper)
    body = {"table": "software", "values": {"status": "DEPRECATED"}}
    url = f"/api/v1/software/{software_payload['uri']}"
    anonymous = client.patch(url, json=body)
    authorised = client.patch(url, json=body, headers=oper)
    assert anonymous.status_code == 401
    assert authorised.status_code == 200


def test_forged_token_is_401_not_403(client, secured, software_payload, forged_keypair, bearer):
    forged, _ = forged_keypair
    response = client.post("/api/v1/software", json=software_payload, headers=bearer(forged))
    assert response.status_code == 401


def test_expired_token_is_rejected(client, secured, software_payload, bearer):
    import time

    headers = bearer(exp=int(time.time()) - 10)
    response = client.post("/api/v1/software", json=software_payload, headers=headers)
    assert response.status_code == 401


def test_malformed_authorization_header_is_401(client, secured, software_payload):
    response = client.post(
        "/api/v1/software", json=software_payload, headers={"Authorization": "Basic abc"}
    )
    assert response.status_code == 401


def test_reads_and_queries_stay_anonymous(client, secured, software_payload, fake_db, bearer):
    """Gating POST /tap/sync would lock every standard VO client out."""
    client.post("/api/v1/software", json=software_payload, headers=bearer())
    assert client.get("/api/v1/software").status_code == 200
    assert client.get(f"/api/v1/software/{software_payload['uri']}").status_code == 200
    assert client.get("/api/v1/tables").status_code == 200
    query = "SELECT source_id, ra FROM ska.continuum_sources"
    synchronous = client.post("/tap/sync", data={"LANG": "ADQL", "QUERY": query})
    job = client.post("/api/v1/jobs", json={"query": query})
    assert synchronous.status_code == 200
    assert job.status_code == 201


def test_a_bad_token_is_rejected_even_on_an_open_endpoint(client, secured, forged_keypair, bearer):
    """An unverifiable credential is an error, never silently anonymous."""
    forged, _ = forged_keypair
    response = client.get("/api/v1/software", headers=bearer(forged))
    assert response.status_code == 401


def test_auth_endpoint_reports_the_policy(client, secured, iam_issuer):
    body = client.get("/api/v1/auth").json()
    assert body["enabled"] is True
    assert body["plugin"] == "iam-groups"
    assert body["issuer"] == iam_issuer
    assert "metadata.delete" in body["gated_operations"]


def test_auth_endpoint_reports_when_disabled(client):
    """Nothing is enforced, so nothing is listed: naming operations here would
    tell a client to go and get a token it will never be asked for."""
    assert client.get("/api/v1/auth").json() == {"enabled": False, "gated_operations": {}}


def test_mutations_are_open_when_auth_is_disabled(client, software_payload):
    """The default deployment must behave exactly as it did before auth existed."""
    created = client.post("/api/v1/software", json=software_payload)
    deleted = client.delete(f"/api/v1/software/{software_payload['uri']}")
    assert created.status_code == 201
    assert deleted.status_code == 200


def test_an_empty_policy_denies_every_write(
    client, auth_settings, stub_iam, iam_issuer, iam_audience, software_payload, bearer
):
    """Auth enabled with an unwritten policy must lock writes, not open them."""
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=EMPTY_ROLES,
        iam_issuer=iam_issuer,
        iam_audience=iam_audience,
    )
    token = bearer()
    created = client.post("/api/v1/software", json=software_payload, headers=token)
    deleted = client.delete(f"/api/v1/software/{software_payload['uri']}", headers=token)
    assert created.status_code == 403
    assert deleted.status_code == 403
