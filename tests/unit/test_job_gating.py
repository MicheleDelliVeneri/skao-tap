"""Which operations the gate enforces, and what changes when a site adds the
job and query ones.

The default set is metadata mutation only, because enforcing job creation or
synchronous querying turns away VO clients that send no token. These tests
pin both halves: the default stays open, and a deployment that opts in really
does close every path to a job.
"""

import json

import pytest
from tapcore.auth import OPERATIONS, QUERY_OPERATIONS, gated_operations

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
    """Auth on, gate set left at its default, serving anonymous VO clients."""
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=JOB_ROLES,
        auth_anonymous_queries=True,
        iam_issuer=iam_issuer,
        iam_audience=iam_audience,
    )


@pytest.fixture
def job_gates(auth_settings, stub_iam, iam_issuer, iam_audience):
    """Auth on, with the job and query operations enforced too.

    Anonymous queries are allowed at the authentication layer, so what these
    tests observe is the *gate* refusing a token-less caller rather than the
    token requirement doing it first.
    """
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=JOB_ROLES,
        auth_gated_operations=JOB_OPERATIONS,
        auth_anonymous_queries=True,
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
    auth_settings(auth_gated_operations="metadata.delete, metadata.ingest , metadata.delete")
    assert gated_operations() == ("metadata.ingest", "metadata.delete")


def test_the_query_operations_are_enforced_as_a_group(auth_settings):
    """Gating a subset is not a weaker policy, it is an incoherent one: the
    caller refused at /tap/async runs the same query at /tap/sync."""
    auth_settings(auth_gated_operations="jobs.create")
    with pytest.raises(LookupError, match=r"query\.sync"):
        gated_operations()


def test_the_whole_query_group_is_accepted(auth_settings):
    auth_settings(auth_gated_operations=JOB_OPERATIONS)
    assert gated_operations() == QUERY_OPERATIONS


def test_the_query_group_composes_with_the_metadata_operations(auth_settings):
    auth_settings(auth_gated_operations=f"metadata.delete,{JOB_OPERATIONS}")
    assert gated_operations() == ("metadata.delete", *QUERY_OPERATIONS)


def test_a_missing_group_member_is_named(auth_settings):
    auth_settings(auth_gated_operations="jobs.create,jobs.mutate,query.sync")
    with pytest.raises(LookupError, match=r"jobs\.delete"):
        gated_operations()


def test_an_unknown_operation_is_refused(auth_settings):
    auth_settings(auth_gated_operations="jobs.create,jobs.rename")
    with pytest.raises(LookupError, match=r"jobs\.rename"):
        gated_operations()


def test_nothing_can_be_gated_explicitly(auth_settings):
    auth_settings(auth_gated_operations="none")
    assert gated_operations() == ()


def test_a_list_that_names_no_operation_is_refused(auth_settings):
    """A typo must not read as "enforce nothing" — that needs the word."""
    auth_settings(auth_gated_operations=" , ,")
    with pytest.raises(LookupError, match="names no operation"):
        gated_operations()


def test_every_operation_is_selectable(auth_settings):
    auth_settings(auth_gated_operations=",".join(OPERATIONS))
    assert gated_operations() == OPERATIONS


# -- the default: querying stays anonymous ----------------------------------


def test_tap_queries_and_jobs_are_open_where_anonymous_queries_are_allowed(
    client, default_gates, fake_db
):
    """The default gate set must not add a second lock on top of the switch."""
    job_id = _create(client)
    queried = client.post("/tap/sync", data={"LANG": "ADQL", "QUERY": QUERY})
    mutated = client.post(
        f"/tap/async/{job_id}/destruction",
        data={"DESTRUCTION": "2030-01-01T00:00:00Z"},
        follow_redirects=False,
    )
    deleted = client.delete(f"/tap/async/{job_id}", follow_redirects=False)
    json_job = client.post("/api/v1/jobs", json={"query": QUERY})
    json_query = client.post("/api/v1/query", json={"query": QUERY})
    assert queried.status_code == 200
    assert mutated.status_code == 303
    assert deleted.status_code == 303
    # the JSON facade is not the VO toolchain: it needs a token to be reached
    # at all, so these are 401 rather than a gate decision
    assert json_job.status_code == 401
    assert json_query.status_code == 401


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
    listed = client.get("/tap/async")
    anonymous = client.get(f"/tap/async/{job_id}")
    owned = client.get(f"/tap/async/{job_id}", headers=bearer(groups=[OPER]))
    assert listed.status_code == 200
    assert anonymous.status_code == 403  # owned by alice
    assert owned.status_code == 200


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
    anonymous_job = client.post("/api/v1/jobs", json={"query": QUERY})
    anonymous_query = client.post("/api/v1/query", json={"query": QUERY})
    created = client.post("/api/v1/jobs", json={"query": QUERY}, headers=headers)
    assert anonymous_job.status_code == 401
    assert anonymous_query.status_code == 401
    assert created.status_code == 201

    job_id = created.json()["job_id"]
    anonymous_delete = client.delete(f"/api/v1/jobs/{job_id}")
    owned_delete = client.delete(f"/api/v1/jobs/{job_id}", headers=headers)
    assert anonymous_delete.status_code == 401
    assert owned_delete.status_code == 204


def test_an_unconfigured_job_role_denies(
    client, auth_settings, stub_iam, iam_issuer, iam_audience, bearer, fake_db
):
    """Enforcing an operation the policy never grants must deny, not open."""
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=json.dumps({"metadata.ingest": {"groups": [OPER]}}),
        auth_gated_operations=JOB_OPERATIONS,
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
