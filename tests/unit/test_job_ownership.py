"""Job ownership and per-user visibility.

Ownership is enforced in the job store rather than at each endpoint, so
these tests go through HTTP on both facades — the UWS resources and the JSON
one — to check the enforcement really covers every path that can reach a job.
"""

import json

import pytest
from tapcore import uws
from tapcore.auth import clear_job_viewer, set_job_viewer
from tapcore.errors import AuthorizationError

QUERY = "SELECT source_id, ra FROM ska.continuum_sources"
ROLES = json.dumps({"metadata.ingest": {"groups": ["/ska/science-metadata/oper"]}})


@pytest.fixture
def secured(auth_settings, stub_iam, iam_issuer, iam_audience):
    """Authenticated, and serving standard VO clients.

    Anonymous queries are on because that is the only way an *ownerless* job
    comes into existence in an authenticated deployment, and half of what
    ownership has to get right is how those behave.
    """
    auth_settings(
        auth_enabled=True,
        auth_plugin="iam-groups",
        auth_roles=ROLES,
        auth_anonymous_queries=True,
        iam_issuer=iam_issuer,
        iam_audience=iam_audience,
    )


@pytest.fixture
def bearer(make_token):
    def build(subject="user-1", **overrides):
        return {"Authorization": f"Bearer {make_token(sub=subject, **overrides)}"}

    return build


# -- the store layer --------------------------------------------------------


def _anonymous_job(client):
    """Create a job without a token.

    Through /tap/async, not the JSON facade: reading metadata through TAP is
    the one thing that stays open to an anonymous caller, so that is where an
    ownerless job can still come from.
    """
    created = client.post(
        "/tap/async", data={"LANG": "ADQL", "QUERY": QUERY}, follow_redirects=False
    )
    assert created.status_code == 303, created.text
    return created.headers["location"].rsplit("/", 1)[-1]


@pytest.fixture
def viewer():
    yield set_job_viewer
    clear_job_viewer()


def test_store_is_unrestricted_without_a_viewer(fake_db):
    """The executor has no request context and must see the whole store."""
    owned = fake_db.add_job(owner_id="someone-else")
    with uws_conn() as conn:
        assert uws.get_job(conn, owned["job_id"])["owner_id"] == "someone-else"
        assert any(j["job_id"] == owned["job_id"] for j in uws.list_jobs(conn))


def test_another_users_job_is_refused(fake_db, viewer):
    owned = fake_db.add_job(owner_id="someone-else")
    viewer("user-1")
    with uws_conn() as conn, pytest.raises(AuthorizationError, match="another user"):
        uws.get_job(conn, owned["job_id"])


def test_own_and_ownerless_jobs_are_visible(fake_db, viewer):
    mine = fake_db.add_job(owner_id="user-1")
    anonymous = fake_db.add_job(owner_id=None)
    theirs = fake_db.add_job(owner_id="someone-else")
    viewer("user-1")
    with uws_conn() as conn:
        listed = {j["job_id"] for j in uws.list_jobs(conn)}
    assert mine["job_id"] in listed
    assert anonymous["job_id"] in listed
    assert theirs["job_id"] not in listed


def test_mutating_another_users_job_is_refused(fake_db, viewer):
    theirs = fake_db.add_job(owner_id="someone-else")
    viewer("user-1")
    with uws_conn() as conn:
        with pytest.raises(AuthorizationError):
            uws.update_job(conn, theirs["job_id"], phase="ABORTED")
        with pytest.raises(AuthorizationError):
            uws.delete_job(conn, theirs["job_id"])
    assert theirs["job_id"] in fake_db.jobs


def uws_conn():
    from tapcore.db import pool

    return pool().connection()


# -- over HTTP, both facades ------------------------------------------------


def test_created_jobs_record_their_owner(client, secured, fake_db, bearer):
    created = client.post("/api/v1/jobs", json={"query": QUERY}, headers=bearer("alice"))
    assert created.status_code == 201
    assert fake_db.jobs[created.json()["job_id"]]["owner_id"] == "alice"


def test_anonymous_jobs_stay_ownerless(client, secured, fake_db):
    job_id = _anonymous_job(client)
    assert fake_db.jobs[job_id]["owner_id"] is None


def test_uws_jobs_record_their_owner(client, secured, fake_db, bearer):
    # not following the 303: it points at the configured public base URL,
    # which is a different origin from the test client, and httpx drops the
    # Authorization header across origins
    created = client.post(
        "/tap/async",
        data={"LANG": "ADQL", "QUERY": QUERY},
        headers=bearer("alice"),
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text
    owners = {j["owner_id"] for j in fake_db.jobs.values()}
    assert "alice" in owners


def test_an_owned_uws_job_is_not_readable_without_the_token(client, secured, fake_db, bearer):
    created = client.post(
        "/tap/async",
        data={"LANG": "ADQL", "QUERY": QUERY},
        headers=bearer("alice"),
        follow_redirects=False,
    )
    job_id = created.headers["location"].rsplit("/", 1)[-1]
    assert client.get(f"/tap/async/{job_id}").status_code == 403
    assert client.get(f"/tap/async/{job_id}", headers=bearer("alice")).status_code == 200


def test_the_viewer_does_not_leak_between_requests(client, secured, fake_db, bearer):
    """A request must never be judged against the previous request's identity.

    The viewer lives in a context variable, and a context variable is only
    guaranteed to be isolated per task — so it is set on every request rather
    than left over from the last one.
    """
    alice = client.post("/api/v1/jobs", json={"query": QUERY}, headers=bearer("alice")).json()

    # bob, immediately after alice, must not inherit her view...
    assert client.get(f"/api/v1/jobs/{alice['job_id']}", headers=bearer("bob")).status_code == 403
    # ...nor may an anonymous caller inherit bob's
    assert fake_db.jobs[_anonymous_job(client)]["owner_id"] is None


def test_one_user_cannot_read_anothers_job(client, secured, fake_db, bearer):
    alice = client.post("/api/v1/jobs", json={"query": QUERY}, headers=bearer("alice"))
    job_id = alice.json()["job_id"]

    as_bob = client.get(f"/api/v1/jobs/{job_id}", headers=bearer("bob"))
    as_alice = client.get(f"/api/v1/jobs/{job_id}", headers=bearer("alice"))
    assert as_bob.status_code == 403
    assert as_alice.status_code == 200


def test_one_user_cannot_read_anothers_job_over_uws(client, secured, fake_db, bearer):
    """The XML facade reaches the same jobs and must enforce the same rule."""
    alice = client.post("/api/v1/jobs", json={"query": QUERY}, headers=bearer("alice"))
    job_id = alice.json()["job_id"]

    assert client.get(f"/tap/async/{job_id}", headers=bearer("bob")).status_code == 403
    assert client.get(f"/tap/async/{job_id}/phase", headers=bearer("bob")).status_code == 403
    assert client.get(f"/tap/async/{job_id}/parameters", headers=bearer("bob")).status_code == 403


def test_one_user_cannot_delete_or_abort_anothers_job(client, secured, fake_db, bearer):
    alice = client.post("/api/v1/jobs", json={"query": QUERY}, headers=bearer("alice"))
    job_id = alice.json()["job_id"]

    aborted = client.post(
        f"/api/v1/jobs/{job_id}/phase", json={"phase": "ABORT"}, headers=bearer("bob")
    )
    deleted = client.delete(f"/api/v1/jobs/{job_id}", headers=bearer("bob"))
    assert aborted.status_code == 403
    assert deleted.status_code == 403
    assert job_id in fake_db.jobs


def test_one_user_cannot_download_anothers_result(client, secured, fake_db, bearer, results_dir):
    import os

    job = fake_db.add_job(phase="COMPLETED", result_mime="application/json", owner_id="alice")
    os.makedirs(os.path.join(results_dir, job["job_id"]), exist_ok=True)
    with open(os.path.join(results_dir, job["job_id"], "result.json"), "w") as handle:
        handle.write("[]")

    assert (
        client.get(f"/api/v1/jobs/{job['job_id']}/result", headers=bearer("bob")).status_code == 403
    )
    assert (
        client.get(f"/api/v1/jobs/{job['job_id']}/result", headers=bearer("alice")).status_code
        == 200
    )


def test_job_lists_show_only_your_own(client, secured, fake_db, bearer):
    alice = client.post("/api/v1/jobs", json={"query": QUERY}, headers=bearer("alice")).json()
    bob = client.post("/api/v1/jobs", json={"query": QUERY}, headers=bearer("bob")).json()
    anonymous_id = _anonymous_job(client)

    listed = {
        j["job_id"] for j in client.get("/api/v1/jobs", headers=bearer("alice")).json()["jobs"]
    }
    assert alice["job_id"] in listed
    assert anonymous_id in listed  # ownerless: nothing to protect
    assert bob["job_id"] not in listed


def test_ownership_is_not_enforced_when_auth_is_disabled(client, fake_db):
    """The unauthenticated deployment must behave exactly as it always has."""
    first = client.post("/api/v1/jobs", json={"query": QUERY}).json()
    fake_db.add_job(owner_id="someone-else")
    listed = {j["job_id"] for j in client.get("/api/v1/jobs").json()["jobs"]}
    assert first["job_id"] in listed
    assert len(listed) == len(fake_db.jobs)
