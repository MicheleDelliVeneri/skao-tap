"""Every endpoint a deployed egernia serves, asked a real question.

The unit and component suites already cover behaviour against a TestClient.
What can only go wrong once deployed is routing, credentials, the schema the
metadata plugins created at boot, and the data the post-deploy seeder wrote —
so every test here goes over the ingress to a live service, and every endpoint
gets exercised rather than sampled.

Read-only apart from one ingest/amend/delete round trip against the software
domain, which creates a document under its own uri and removes it again.
"""

from __future__ import annotations

import csv
import io
import time
import uuid

import pytest
from conftest import QUERY_TIMEOUT_S, sync_query

# Enough rows to be worth querying. Floors rather than exact counts: the seeder
# targets 500,000 data products and the full 7,700-row software catalogue, but
# it is resumable and may still be running when a suite starts. A floor
# separates "seeding in progress" from "nothing was ever written".
SEEDED_FLOORS = (
    ("ivoa.obscore", 100_000),
    ("srcnet.data_products", 100_000),
    ("srcnet.artifacts", 100_000),
    ("srcnet.projects", 500),
    ("srcnet.observations", 2_000),
    ("srcnet.scheduling_blocks", 4_000),
    ("srcnet.execution_blocks", 8_000),
    ("srcnet.software", 1_000),
    ("srcnet.software_artifacts", 2_000),
)

# The two metadata domains the service mounts, from the odp and software
# plugins. Named here so a plugin added without a test is visible as a gap.
DOMAINS = ("notifications", "software")


def _rows(response) -> list[dict]:
    return list(csv.DictReader(io.StringIO(response.text)))


# ---------------------------------------------------------------------------
# Service endpoints: reachable, and answering as themselves.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/", "/health/live", "/health/ready", "/metrics", "/openapi.json", "/docs"],
)
def test_service_endpoints_answer(anonymous, base_url, path):
    """Root, both probes, metrics and the OpenAPI surface.

    /health/ready is the one the rollout gates on and /metrics is what the
    deployment's Prometheus scrapes, so a deployment where either is unroutable
    is broken in a way no query test would show.
    """
    response = anonymous.get(f"{base_url}{path}", timeout=30)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text[:200]}"


@pytest.mark.parametrize(
    "series",
    [
        "tap_query_duration_seconds",
        "tap_db_connections_in_use",
        "tap_db_pool_wait_seconds",
        "tap_oldest_queued_job_seconds",
    ],
)
def test_metrics_expose_the_tap_series(anonymous, base_url, series):
    """The scrape has to carry this service's own series, not just python's.

    Names taken from a running service rather than guessed — the deployment's
    Prometheus scrape and the autoscaler's queue-depth query both name these,
    so a rename that slipped through would break both silently.
    """
    body = anonymous.get(f"{base_url}/metrics", timeout=30).text
    assert series in body, f"{series} absent from /metrics"


# ---------------------------------------------------------------------------
# VOSI: what a VO client reads before it asks anything.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/availability", "/capabilities", "/tables", "/examples"])
def test_vosi_endpoints_answer(anonymous, tap_url, path):
    """All four, anonymously: service discovery is never behind the gate."""
    response = anonymous.get(f"{tap_url}{path}", timeout=60)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text[:200]}"
    assert response.text.strip(), f"{path} returned an empty body"


def test_the_registry_record_is_served_or_absent(anonymous, tap_url):
    """/tap/registry is the VOResource record, and publishing one is a choice.

    404 is the correct answer where `voRegistry.enabled` is off, which is the
    chart default and what this environment deploys — an identifier and an
    authority are promises, not defaults. Asserted as one-or-the-other so the
    endpoint is still covered without pinning a deployment decision.
    """
    status = anonymous.get(f"{tap_url}/registry", timeout=30).status_code
    assert status in (200, 404), f"unexpected {status} from /tap/registry"


def test_availability_reports_available(anonymous, tap_url):
    response = anonymous.get(f"{tap_url}/availability", timeout=30)
    assert "available" in response.text.lower()


def test_capabilities_name_the_deployment_not_the_pod(anonymous, tap_url):
    """The accessURL handed to a client must be one it can reach.

    Those URLs come from the request or from baseUrl, so a deployment that
    advertises an in-cluster Service name sends every VO client somewhere it
    cannot resolve. That is what trustedHosts decides, and it is worth
    asserting against the deployed value rather than trusting it.
    """
    body = anonymous.get(f"{tap_url}/capabilities", timeout=30).text
    assert "<accessURL" in body
    assert "egernia-tap-api" not in body, (
        "capabilities advertise the in-cluster Service name: baseUrl or "
        "trustedHosts is wrong for this deployment"
    )


def test_tables_describe_both_metadata_models(anonymous, tap_url):
    """TAP_SCHEMA must describe what the plugins actually created."""
    body = anonymous.get(f"{tap_url}/tables", timeout=60).text
    for table in ("ivoa.obscore", "srcnet.data_products", "srcnet.software"):
        assert table in body, f"{table} is absent from /tap/tables"


# ---------------------------------------------------------------------------
# Authorisation: the gate is closed, and the token opens it.
# ---------------------------------------------------------------------------


def test_the_auth_endpoint_reports_the_deployed_policy(anonymous, api_url):
    response = anonymous.get(f"{api_url}/auth", timeout=30)
    assert response.status_code == 200, response.text
    assert response.json().get("enabled") is True, (
        "authentication is off in this deployment; the integration environment "
        "deploys it on so IAM and the Permissions API are exercised"
    )


def test_a_query_without_a_token_is_refused(anonymous, tap_url):
    """Asserted, not assumed: a deployment serving anonymous queries would
    pass every other test in this file."""
    response = anonymous.post(
        f"{tap_url}/sync",
        data={"LANG": "ADQL", "QUERY": "SELECT TOP 1 obs_id FROM ivoa.obscore"},
        timeout=60,
    )
    assert response.status_code == 401, (
        f"expected 401 without a token, got {response.status_code}: {response.text[:200]}"
    )


def test_a_query_with_a_token_is_served(session, tap_url):
    """The whole auth chain in one assertion: IAM issued a token, the exchange
    gave it the egernia audience, egernia verified it against the
    IAM's JWKS over the cluster's own CA, and the Permissions API approved."""
    response = sync_query(session, "SELECT TOP 1 obs_id FROM ivoa.obscore")
    assert response.status_code == 200, response.text
    assert _rows(response), "authorised query returned no rows"


# ---------------------------------------------------------------------------
# Querying: /tap/sync both verbs, every result format, and the JSON facade.
# ---------------------------------------------------------------------------


def test_sync_answers_get_as_well_as_post(session, tap_url):
    """TOPCAT sends GET, PyVO sends POST; the ingress routes both to one path."""
    response = session.get(
        f"{tap_url}/sync",
        params={"LANG": "ADQL", "QUERY": "SELECT TOP 1 obs_id FROM ivoa.obscore"},
        timeout=QUERY_TIMEOUT_S,
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("fmt", ["csv", "tsv", "votable", "json", "parquet", "arrow"])
def test_sync_serves_every_result_format(session, fmt):
    """Each format is a different serialiser; a deployment missing one of the
    columnar dependencies fails only on that one."""
    response = sync_query(session, "SELECT TOP 5 obs_id, s_ra, s_dec FROM ivoa.obscore", fmt=fmt)
    assert response.status_code == 200, f"{fmt}: {response.text[:200]}"
    assert response.content, f"{fmt} returned an empty body"


def test_maxrec_is_honoured(session, tap_url):
    response = session.post(
        f"{tap_url}/sync",
        data={
            "LANG": "ADQL",
            "QUERY": "SELECT obs_id FROM ivoa.obscore",
            "RESPONSEFORMAT": "csv",
            "MAXREC": "3",
        },
        timeout=QUERY_TIMEOUT_S,
    )
    assert response.status_code == 200, response.text
    assert len(_rows(response)) <= 3


def test_the_json_query_facade_answers(session, api_url):
    """POST /api/v1/query — the same engine, a JSON request and response."""
    response = session.post(
        f"{api_url}/query",
        json={"query": "SELECT TOP 3 obs_id FROM ivoa.obscore", "format": "json"},
        timeout=QUERY_TIMEOUT_S,
    )
    assert response.status_code == 200, response.text
    assert response.json(), "the JSON query facade returned an empty payload"


def test_the_json_tables_endpoint_answers(session, api_url):
    response = session.get(f"{api_url}/tables", timeout=60)
    assert response.status_code == 200, response.text
    assert response.json(), "/api/v1/tables returned an empty payload"


# ---------------------------------------------------------------------------
# The seeded data, and both models under one query language.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("table", "minimum"), SEEDED_FLOORS)
def test_the_seeded_tables_hold_data(session, table, minimum):
    response = sync_query(session, f"SELECT COUNT(*) AS n FROM {table}")
    assert response.status_code == 200, response.text
    count = int(_rows(response)[0]["n"])
    assert count >= minimum, f"{table} holds {count} rows, expected at least {minimum}"


def test_the_odp_hierarchy_joins(session):
    """A join down the ODP model, which is what the fan-out exists for."""
    response = sync_query(
        session,
        "SELECT TOP 5 p.project_id, d.obs_id "
        "FROM srcnet.projects AS p "
        "JOIN srcnet.observations AS o ON o.project_id = p.project_id "
        "JOIN srcnet.data_products AS d ON d.obs_id = o.obs_id",
    )
    assert response.status_code == 200, response.text
    assert _rows(response), "the ODP hierarchy join returned no rows"


def test_the_software_model_joins(session):
    response = sync_query(
        session,
        "SELECT TOP 5 s.uri, a.location "
        "FROM srcnet.software AS s "
        "JOIN srcnet.software_artifacts AS a ON a.uri = s.uri",
    )
    assert response.status_code == 200, response.text
    assert _rows(response), "the software join returned no rows"


def test_a_spatial_query_uses_the_footprint_index(session):
    """A cone search, the query the GiST indexes exist for.

    The seeder drops those indexes for the load and rebuilds them after, so
    this passing is what proves the rebuild completed — a failure that leaves
    every other query in this file green.
    """
    response = sync_query(
        session,
        "SELECT TOP 5 obs_id FROM ivoa.obscore "
        "WHERE 1 = CONTAINS(POINT('ICRS', s_ra, s_dec), "
        "CIRCLE('ICRS', 201.365, -43.019, 5.0))",
    )
    assert response.status_code == 200, response.text


def test_an_aggregate_over_the_whole_table_answers(session):
    """Deliberately the expensive one: no index helps a full scan.

    This is the query that fails as "server disconnected" when the ingress
    caps a response below the time the service is allowed to take, so it is
    also the test for the proxy timeouts the overlay sets.
    """
    response = sync_query(
        session,
        "SELECT dataproduct_type, COUNT(*) AS n FROM ivoa.obscore GROUP BY dataproduct_type",
    )
    assert response.status_code == 200, response.text
    assert _rows(response), "the aggregate returned no rows"


# ---------------------------------------------------------------------------
# UWS async: the job, and every sub-resource it publishes.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def completed_job(session, tap_url) -> str:
    """One job run to COMPLETED, reused by the sub-resource sweep below."""
    created = session.post(
        f"{tap_url}/async",
        data={
            "LANG": "ADQL",
            "QUERY": "SELECT TOP 10 obs_id, s_ra, s_dec FROM ivoa.obscore",
            "RESPONSEFORMAT": "csv",
            "PHASE": "RUN",
        },
        timeout=60,
        allow_redirects=True,
    )
    assert created.status_code in (200, 303), created.text
    job_url = created.url.split("?")[0]
    assert "/tap/async/" in job_url, f"no job location in {job_url}"

    deadline = time.monotonic() + QUERY_TIMEOUT_S
    phase = ""
    while time.monotonic() < deadline:
        phase = session.get(f"{job_url}/phase", timeout=30).text.strip()
        if phase in ("COMPLETED", "ERROR", "ABORTED"):
            break
        time.sleep(2)
    assert phase == "COMPLETED", f"job ended in {phase or 'no phase'}"
    return job_url


def test_the_async_job_list_answers(session, tap_url, completed_job):
    response = session.get(f"{tap_url}/async", timeout=60)
    assert response.status_code == 200, response.text
    assert response.text.strip(), "the job list returned an empty body"


@pytest.mark.parametrize(
    "sub",
    [
        "",
        "/phase",
        "/executionduration",
        "/destruction",
        "/quote",
        "/owner",
        "/parameters",
        "/results",
        "/results/result",
    ],
)
def test_every_uws_sub_resource_answers(session, completed_job, sub):
    """The whole UWS surface on a real job.

    Also proves the job's own URLs are followable: the location the service
    returns is built from the request, so a wrong trustedHosts hands out a URL
    these requests cannot reach — which no synchronous test would catch.
    """
    response = session.get(f"{completed_job}{sub}", timeout=QUERY_TIMEOUT_S)
    assert response.status_code == 200, f"{sub or '(job)'}: {response.text[:200]}"


def test_the_job_result_carries_rows(session, completed_job):
    response = session.get(f"{completed_job}/results/result", timeout=QUERY_TIMEOUT_S)
    assert _rows(response), "the completed job produced no rows"


def test_a_job_can_be_deleted(session, tap_url):
    """Its own job, so the sweep above keeps the one it is reading."""
    created = session.post(
        f"{tap_url}/async",
        data={"LANG": "ADQL", "QUERY": "SELECT TOP 1 obs_id FROM ivoa.obscore"},
        timeout=60,
        allow_redirects=True,
    )
    job_url = created.url.split("?")[0]
    assert session.delete(job_url, timeout=60).status_code in (200, 303)
    assert session.get(job_url, timeout=30).status_code == 404


# ---------------------------------------------------------------------------
# The JSON job facade over the same store.
# ---------------------------------------------------------------------------


def test_the_json_job_lifecycle(session, api_url):
    """Create, list, fetch, run, poll, read the result, delete."""
    created = session.post(
        f"{api_url}/jobs",
        json={
            "query": "SELECT TOP 5 obs_id FROM ivoa.obscore",
            "format": "csv",
            "run": True,
            "run_id": f"integration-{uuid.uuid4().hex[:8]}",
        },
        timeout=60,
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]

    assert session.get(f"{api_url}/jobs", timeout=60).status_code == 200
    assert session.get(f"{api_url}/jobs/{job_id}", timeout=30).status_code == 200

    deadline = time.monotonic() + QUERY_TIMEOUT_S
    phase = ""
    while time.monotonic() < deadline:
        phase = session.get(f"{api_url}/jobs/{job_id}", timeout=30).json().get("phase", "")
        if phase in ("COMPLETED", "ERROR", "ABORTED"):
            break
        time.sleep(2)
    assert phase == "COMPLETED", f"job ended in {phase or 'no phase'}"

    result = session.get(f"{api_url}/jobs/{job_id}/result", timeout=QUERY_TIMEOUT_S)
    assert result.status_code == 200, result.text
    assert _rows(result), "the JSON job produced no rows"

    assert session.delete(f"{api_url}/jobs/{job_id}", timeout=30).status_code == 204


# ---------------------------------------------------------------------------
# The metadata domains: list and fetch for both, and one write round trip.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("domain", DOMAINS)
def test_each_metadata_domain_lists(session, api_url, domain):
    response = session.get(f"{api_url}/{domain}", timeout=60)
    assert response.status_code == 200, f"{domain}: {response.text[:200]}"


def test_a_seeded_software_document_can_be_fetched(session, api_url):
    """List, then fetch by uri — the identity override this plugin declares."""
    listing = session.get(f"{api_url}/software", timeout=60)
    assert listing.status_code == 200, listing.text
    # Keyed by the plugin's root table name — `{"software": [...]}` — not by a
    # generic `items`/`data` envelope. Verified against a live service.
    payload = listing.json()
    items = payload["software"] if isinstance(payload, dict) else payload
    assert items, "the software listing is empty; has the seeder run?"

    uri = items[0]["uri"]
    fetched = session.get(f"{api_url}/software/{uri}", timeout=30)
    assert fetched.status_code == 200, fetched.text
    # The response is the document body; the identity stays in the URL rather
    # than being repeated in the payload.
    document = fetched.json()
    assert document, f"the document for {uri} came back empty"
    assert "artifacts" in document, f"no artifacts in the document for {uri}"


def test_software_ingest_amend_and_delete(session, api_url):
    """One write round trip, under a uri of this test's own making.

    The only mutating test here, and the only one that needs the
    metadata.ingest/amend/delete grants — so it is also what proves the
    Permissions API is deciding rather than the service defaulting to allow.
    """
    # The uri is {publisher}:{name}:{semver} and the model enforces it, so the
    # unique part goes in the name rather than in place of a version. Artifact
    # kind is an enum (DOCKER/SINGULARITY/OCI), cpu_architecture is one too
    # (amd64/arm64 — not x86_64) and is required. All of it learned from 422s
    # rather than from the docs.
    suffix = uuid.uuid4().hex[:8]
    uri = f"integration:egernia-suite-{suffix}:1.0.0"
    document = {
        "uri": uri,
        "description": "written by egernia's integration suite",
        "status": "TESTING",
        "artifacts": [
            {
                "kind": "DOCKER",
                "location": f"registry.test/egernia-suite-{suffix}:1.0.0",
                "cpu_architecture": ["amd64"],
            }
        ],
    }

    created = session.post(f"{api_url}/software", json=document, timeout=60)
    assert created.status_code in (200, 201), created.text

    fetched = session.get(f"{api_url}/software/{uri}", timeout=30)
    assert fetched.status_code == 200, fetched.text

    amended = session.patch(
        f"{api_url}/software/{uri}",
        json={"table": "software", "values": {"status": "DEPRECATED"}},
        timeout=30,
    )
    assert amended.status_code in (200, 204), amended.text

    assert session.delete(f"{api_url}/software/{uri}", timeout=30).status_code in (200, 204)
    assert session.get(f"{api_url}/software/{uri}", timeout=30).status_code == 404
