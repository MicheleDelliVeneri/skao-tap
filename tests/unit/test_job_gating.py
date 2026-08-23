"""Which operations the gate enforces, and what changes when a site adds the
job and query ones.

The default set is metadata mutation only, because enforcing job creation or
synchronous querying turns away VO clients that send no token. These tests
pin both halves: the default stays open, and a deployment that opts in really
does close every path to a job.
"""

import json

import pytest
from tapcore.auth import OPERATIONS, gated_operations

QUERY = "SELECT source_id, ra FROM ska.continuum_sources"

OPER = "/ska/science-metadata/oper"
JOB_ROLES = json.dumps(
    {
        "metadata.ingest": {"groups": [OPER]},
        "jobs.create": {"groups": [OPER]},
        "jobs.mutate": {"groups": [OPER]},
        "jobs.delete": {"groups": [OPER]},
        "query.sync": {"groups": [OPER]},
    }
)
JOB_OPERATIONS = "jobs.create,jobs.mutate,jobs.delete,query.sync"


@pytest.fixture
def bearer(make_token):
    def build(subject="alice", **overrides):
        return {"Authorization": f"Bearer {make_token(sub=subject, **overrides)}"}

    return build


@pytest.fixture
def default_gates(auth_settings, stub_iam, iam_issuer, iam_audience):
    """Auth on, gate set left at its default."""
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=JOB_ROLES,
        iam_issuer=iam_issuer,
        iam_audience=iam_audience,
    )


@pytest.fixture
def job_gates(auth_settings, stub_iam, iam_issuer, iam_audience):
    """Auth on, with the job and query operations enforced too."""
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=JOB_ROLES,
        auth_gated_operations=JOB_OPERATIONS,
        iam_issuer=iam_issuer,
        iam_audience=iam_audience,
    )


def _create(client, headers=None):
    response = client.post(
        "/tap/async",
        data={"LANG": "ADQL", "QUERY": QUERY},
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"].rsplit("/", 1)[-1]


# -- resolving the gate set -------------------------------------------------


def test_the_default_gate_set_is_metadata_only():
    assert gated_operations() == ("metadata.ingest", "metadata.amend", "metadata.delete")


def test_the_gate_set_is_ordered_and_deduplicated(auth_settings):
    auth_settings(auth_gated_operations="query.sync, jobs.create , jobs.create")
    assert gated_operations() == ("jobs.create", "query.sync")


def test_an_unknown_operation_is_refused(auth_settings):
    auth_settings(auth_gated_operations="jobs.create,jobs.rename")
    with pytest.raises(LookupError, match=r"jobs\.rename"):
        gated_operations()


def test_every_operation_is_selectable(auth_settings):
    auth_settings(auth_gated_operations=",".join(OPERATIONS))
    assert gated_operations() == OPERATIONS


# -- the default: querying stays anonymous ----------------------------------


def test_jobs_and_queries_stay_anonymous_by_default(client, default_gates, fake_db):
    """Enabling auth must not, on its own, lock standard VO clients out."""
    job_id = _create(client)
    assert client.post("/tap/sync", data={"LANG": "ADQL", "QUERY": QUERY}).status_code == 200
    mutated = client.post(
        f"/tap/async/{job_id}/destruction",
        data={"DESTRUCTION": "2030-01-01T00:00:00Z"},
        follow_redirects=False,
    )
    assert mutated.status_code == 303
    assert client.delete(f"/tap/async/{job_id}", follow_redirects=False).status_code == 303
    assert client.post("/api/v1/jobs", json={"query": QUERY}).status_code == 201
    assert client.post("/api/v1/query", json={"query": QUERY}).status_code == 200


# -- opting in: every path to a job needs a token ---------------------------


def test_job_creation_needs_a_token(client, job_gates, fake_db):
    response = client.post(
        "/tap/async", data={"LANG": "ADQL", "QUERY": QUERY}, follow_redirects=False
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_job_creation_needs_the_configured_group(client, job_gates, fake_db, bearer):
    response = client.post(
        "/tap/async",
        data={"LANG": "ADQL", "QUERY": QUERY},
        headers=bearer(groups=["/ska/other"]),
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_job_creation_succeeds_with_the_group(client, job_gates, fake_db, bearer):
    job_id = _create(client, headers=bearer(groups=[OPER]))
    assert fake_db.jobs[job_id]["owner_id"] == "alice"


def test_reading_jobs_stays_open(client, job_gates, fake_db, bearer):
    """Only the mutating verbs are gated. Reads are governed by ownership, so
    an anonymous caller still gets a job list — just not other people's jobs."""
    job_id = _create(client, headers=bearer(groups=[OPER]))
    assert client.get("/tap/async").status_code == 200
    assert client.get(f"/tap/async/{job_id}").status_code == 403  # owned by alice
    assert client.get(f"/tap/async/{job_id}", headers=bearer(groups=[OPER])).status_code == 200


def test_deleting_a_job_needs_a_token(client, job_gates, fake_db, bearer):
    headers = bearer(groups=[OPER])
    job_id = _create(client, headers=headers)
    anonymous = client.delete(f"/tap/async/{job_id}", follow_redirects=False)
    assert anonymous.status_code == 401
    owned = client.delete(f"/tap/async/{job_id}", headers=headers, follow_redirects=False)
    assert owned.status_code == 303


def test_delete_via_post_action_is_gated_too(client, job_gates, fake_db, bearer):
    """POST ...?ACTION=DELETE is the same destruction by another spelling."""
    job_id = _create(client, headers=bearer(groups=[OPER]))
    anonymous = client.post(
        f"/tap/async/{job_id}", data={"ACTION": "DELETE"}, follow_redirects=False
    )
    assert anonymous.status_code == 401


def test_mutating_a_job_needs_a_token(client, job_gates, fake_db, bearer):
    job_id = _create(client, headers=bearer(groups=[OPER]))
    anonymous = client.post(f"/tap/async/{job_id}/phase", data={"PHASE": "RUN"})
    assert anonymous.status_code == 401


def test_synchronous_querying_is_gated(client, job_gates, fake_db, bearer):
    anonymous = client.post("/tap/sync", data={"LANG": "ADQL", "QUERY": QUERY})
    authorised = client.post(
        "/tap/sync", data={"LANG": "ADQL", "QUERY": QUERY}, headers=bearer(groups=[OPER])
    )
    assert anonymous.status_code == 401
    assert authorised.status_code == 200


def test_the_json_facade_is_gated_on_the_same_operations(client, job_gates, fake_db, bearer):
    headers = bearer(groups=[OPER])
    assert client.post("/api/v1/jobs", json={"query": QUERY}).status_code == 401
    assert client.post("/api/v1/query", json={"query": QUERY}).status_code == 401
    created = client.post("/api/v1/jobs", json={"query": QUERY}, headers=headers)
    assert created.status_code == 201
    job_id = created.json()["job_id"]
    assert client.delete(f"/api/v1/jobs/{job_id}").status_code == 401
    assert client.delete(f"/api/v1/jobs/{job_id}", headers=headers).status_code == 204


def test_an_unconfigured_job_role_denies(
    client, auth_settings, stub_iam, iam_issuer, iam_audience, bearer, fake_db
):
    """Enforcing an operation the policy never grants must deny, not open."""
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=json.dumps({"metadata.ingest": {"groups": [OPER]}}),
        auth_gated_operations="jobs.create",
        iam_issuer=iam_issuer,
        iam_audience=iam_audience,
    )
    response = client.post(
        "/tap/async",
        data={"LANG": "ADQL", "QUERY": QUERY},
        headers=bearer(groups=[OPER]),
        follow_redirects=False,
    )
    assert response.status_code == 403


# -- what the service advertises -------------------------------------------


def test_auth_endpoint_reports_the_enforced_operations(client, job_gates):
    body = client.get("/api/v1/auth").json()
    assert set(body["gated_operations"]) == {
        "jobs.create",
        "jobs.mutate",
        "jobs.delete",
        "query.sync",
    }
    assert "POST /tap/async" in body["gated_operations"]["jobs.create"]


def test_auth_endpoint_omits_operations_that_are_not_enforced(client, default_gates):
    body = client.get("/api/v1/auth").json()
    assert set(body["gated_operations"]) == {
        "metadata.ingest",
        "metadata.amend",
        "metadata.delete",
    }
