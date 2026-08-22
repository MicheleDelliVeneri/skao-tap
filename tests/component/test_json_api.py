"""Component tests for the /api/v1 JSON interface: notification ingestion
validated by ska-src-mm-notification, automatic srcnet schema, JSON queries
and the JSON job facade."""

import copy

import httpx
import pytest
import pyvo
from ska_src_mm_notification.models.schemas.srcnet_ingestion import SRC_INGESTION_EXAMPLE

pytestmark = pytest.mark.component


def _api(tap_service: str) -> str:
    return tap_service.rsplit("/tap", 1)[0] + "/api/v1"


def test_ingest_example_notification(tap_service):
    response = httpx.post(f"{_api(tap_service)}/notifications", json=SRC_INGESTION_EXAMPLE)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "ingested"
    assert body["project_id"] == "project12314"
    assert body["rows"]["srcnet.artifacts"] == 2

    # idempotent: re-posting upserts instead of duplicating
    again = httpx.post(f"{_api(tap_service)}/notifications", json=SRC_INGESTION_EXAMPLE)
    assert again.status_code == 201

    listing = httpx.get(f"{_api(tap_service)}/notifications").json()
    (project,) = [p for p in listing["projects"] if p["project_id"] == "project12314"]
    assert project["artifacts"] == 2


def test_artifact_counts_not_inflated_by_multiple_products(tap_service):
    doubled = copy.deepcopy(SRC_INGESTION_EXAMPLE)
    doubled["project_id"] = "project-multi"
    block = doubled["observations"][0]["scheduling_blocks"][0]["execution_blocks"][0]
    second = copy.deepcopy(block["data_products"][0])
    second["product_id"] = "SKAO-SECOND"
    for artifact in second["artifacts"]:
        artifact["artifact_id"] += "_b"
    block["data_products"].append(second)

    response = httpx.post(f"{_api(tap_service)}/notifications", json=doubled)
    assert response.status_code == 201
    listing = httpx.get(f"{_api(tap_service)}/notifications").json()
    (project,) = [p for p in listing["projects"] if p["project_id"] == "project-multi"]
    assert project["data_products"] == 2
    assert project["artifacts"] == 4  # not 8: no join multiplication


def test_invalid_notification_rejected_with_pydantic_errors(tap_service):
    bad = copy.deepcopy(SRC_INGESTION_EXAMPLE)
    del bad["group_ids"]  # required by the model
    response = httpx.post(f"{_api(tap_service)}/notifications", json=bad)
    assert response.status_code == 422
    assert any("group_ids" in str(e["loc"]) for e in response.json()["detail"])


def test_cross_field_validation_from_library(tap_service):
    bad = copy.deepcopy(SRC_INGESTION_EXAMPLE)
    product = bad["observations"][0]["scheduling_blocks"][0]["execution_blocks"][0][
        "data_products"
    ][0]
    product["em_min"], product["em_max"] = 1.0, 0.15  # em_min > em_max
    response = httpx.post(f"{_api(tap_service)}/notifications", json=bad)
    assert response.status_code == 422
    assert "em_min" in response.text


def test_notification_roundtrip(tap_service):
    httpx.post(f"{_api(tap_service)}/notifications", json=SRC_INGESTION_EXAMPLE)
    document = httpx.get(f"{_api(tap_service)}/notifications/project12314").json()
    assert document["project_title"] == "HI Survey Project"
    observation = document["observations"][0]
    product = observation["scheduling_blocks"][0]["execution_blocks"][0]["data_products"][0]
    assert product["product_id"] == "SKAO-19571257111"
    assert {a["artifact_id"] for a in product["artifacts"]} == {
        "obs12345_cube_1",
        "obs12345_cube_2",
    }
    missing = httpx.get(f"{_api(tap_service)}/notifications/nope")
    assert missing.status_code == 404
    assert missing.json()["error"] == "NotFoundError"


def test_ingested_metadata_queryable_via_tap_adql(tap_service):
    """The generated srcnet tables are TAP_SCHEMA-registered: PyVO + ADQL work."""
    httpx.post(f"{_api(tap_service)}/notifications", json=SRC_INGESTION_EXAMPLE)
    svc = pyvo.dal.TAPService(tap_service)
    table = svc.search(
        "SELECT p.product_id, a.artifact_id, a.access_estsize "
        "FROM srcnet.data_products AS p "
        "JOIN srcnet.artifacts AS a ON p.project_id = a.project_id "
        "AND p.eb_id = a.eb_id AND p.product_id = a.product_id "
        "WHERE p.dataproduct_type = 'cube' AND p.project_id = 'project12314'"
    ).to_table()
    assert len(table) == 2
    assert "srcnet.artifacts" in set(svc.tables.keys())


def test_database_enforces_model_constraints(tap_service, database_url):
    """The CHECKs generated from the pydantic constraints hold in PostgreSQL."""
    import psycopg

    with psycopg.connect(database_url) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO srcnet.projects (schema_version, project_id, group_ids)"
            " VALUES ('2.0', 'p1', '[]'::jsonb)"
        )
        conn.execute(
            "UPDATE srcnet.projects SET data_rights = 'not-a-right' WHERE project_id = 'p1'"
        )


def test_json_query_endpoint(tap_service):
    response = httpx.post(
        f"{_api(tap_service)}/query",
        json={"query": "SELECT TOP 2 source_name FROM ska.continuum_sources", "maxrec": 5},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OK"
    assert body["metadata"][0]["name"] == "source_name"
    assert len(body["data"]) == 2


def test_json_query_errors_are_json_not_votable(tap_service):
    response = httpx.post(f"{_api(tap_service)}/query", json={"query": "SELECT * FROM uws.jobs"})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"] == "UsageError"


def test_json_job_lifecycle(tap_service):
    api = _api(tap_service)
    created = httpx.post(
        f"{api}/jobs",
        json={
            "query": "SELECT source_id FROM ska.continuum_sources",
            "format": "csv",
            "run": True,
        },
    )
    assert created.status_code == 201
    job = created.json()
    assert job["phase"] in ("QUEUED", "EXECUTING", "COMPLETED")

    url = f"{api}/jobs/{job['job_id']}"
    for _ in range(100):
        job = httpx.get(url).json()
        if job["phase"] in ("COMPLETED", "ERROR", "ABORTED"):
            break
        import time

        time.sleep(0.2)
    assert job["phase"] == "COMPLETED"
    assert job["result"]["mime"] == "text/csv"

    result = httpx.get(f"{url}/result")
    assert result.status_code == 200
    assert result.text.splitlines()[0] == "source_id"

    listing = httpx.get(f"{api}/jobs", params={"phase": "COMPLETED"}).json()
    assert job["job_id"] in {j["job_id"] for j in listing["jobs"]}

    deleted = httpx.delete(url)
    assert deleted.status_code == 204
    assert httpx.get(url).status_code == 404


def test_json_job_invalid_query_rejected_at_creation(tap_service):
    response = httpx.post(f"{_api(tap_service)}/jobs", json={"query": "SELEC nonsense"})
    assert response.status_code == 400
    assert response.json()["error"] == "QueryParseError"


def test_json_job_abort(tap_service):
    api = _api(tap_service)
    job = httpx.post(
        f"{api}/jobs", json={"query": "SELECT source_id FROM ska.continuum_sources"}
    ).json()
    aborted = httpx.post(f"{api}/jobs/{job['job_id']}/phase", json={"phase": "ABORT"}).json()
    assert aborted["phase"] == "ABORTED"
    httpx.delete(f"{api}/jobs/{job['job_id']}")


def test_json_tables_metadata(tap_service):
    body = httpx.get(f"{_api(tap_service)}/tables").json()
    names = {t["name"] for t in body["tables"]}
    assert {"ska.continuum_sources", "srcnet.artifacts", "tap_schema.columns"} <= names
    artifacts = next(t for t in body["tables"] if t["name"] == "srcnet.artifacts")
    assert any(c["name"] == "access_url" for c in artifacts["columns"])


def test_openapi_documents_json_api(tap_service):
    root = tap_service.rsplit("/tap", 1)[0]
    spec = httpx.get(f"{root}/openapi.json").json()
    assert "/api/v1/notifications" in spec["paths"]
    assert "/api/v1/query" in spec["paths"]
