"""Component tests for the /api/v1 JSON interface: notification ingestion
validated by ska-src-mm-notification, automatic srcnet schema, JSON queries
and the JSON job facade."""

import copy

import httpx
import pytest
import pyvo
from ska_src_mm_notification.models.schemas.srcnet_ingestion import (
    SRC_INGESTION_EXAMPLE,
)

pytestmark = pytest.mark.component


def _api(tap_service: str) -> str:
    return tap_service.rsplit("/tap", 1)[0] + "/api/v1"


def test_notification_built_with_library_builder_and_queried_via_tap(tap_service):
    """The full producer-to-archive path: build the notification with the
    library's own NotificationBuilder (what a data producer runs), send it,
    and query the ingested metadata back through TAP/ADQL."""
    from ska_src_mm_notification.builder import NotificationBuilder
    from ska_src_mm_notification.models.schemas.srcnet_ingestion import Artifact, DataProduct

    product = DataProduct(
        product_id="builder-prod-1",
        o_ucd="phot.flux",
        dataproduct_type="cube",
        calib_level=2,
        target_name="Builder Target",
        artifacts=[
            Artifact(
                artifact_id="builder-art-1",
                access_url="https://example.org/cube.fits",
                access_format="application/fits",
                access_estsize=123,
            )
        ],
    )
    builder = NotificationBuilder().create_simple_notification(
        project_id="component-builder",
        group_ids=["group-1"],
        obs_id="obs-b1",
        obs_title="Builder observation",
        eb_id="eb-b1",
        data_products=[product],
        project_title="Builder project",
        pi_name="Component PI",
    )
    payload = builder.build_dict()

    response = httpx.post(f"{_api(tap_service)}/notifications", json=payload, timeout=30)
    assert response.status_code == 201, response.text
    rows = response.json()["rows"]
    assert rows["srcnet.projects"] == 1
    assert rows["srcnet.data_products"] == 1
    assert rows["srcnet.artifacts"] == 1

    document = httpx.get(f"{_api(tap_service)}/notifications/component-builder", timeout=10).json()
    assert document["project_title"] == "Builder project"
    (observation,) = document["observations"]
    assert observation["obs_id"] == "obs-b1"

    sync = httpx.post(
        f"{tap_service}/sync",
        data={
            "QUERY": (
                "SELECT p.project_id, a.access_url"
                " FROM srcnet.projects AS p"
                " JOIN srcnet.artifacts AS a ON a.project_id = p.project_id"
                " WHERE p.project_id = 'component-builder'"
            ),
            "LANG": "ADQL",
            "RESPONSEFORMAT": "csv",
        },
        timeout=30,
    )
    assert sync.status_code == 200
    assert "component-builder,https://example.org/cube.fits" in sync.text


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


def test_job_list_rejects_non_positive_last(tap_service):
    response = httpx.get(f"{_api(tap_service)}/jobs", params={"last": 0})
    assert response.status_code == 400
    assert response.json()["error"] == "UsageError"
    uws_response = httpx.get(f"{tap_service}/async", params={"LAST": "0"})
    assert uws_response.status_code == 400
    assert httpx.get(f"{tap_service}/async", params={"LAST": "1"}).status_code == 200


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


def test_full_metadata_notification_roundtrip(tap_service):
    """Send a notification with EVERY DataProduct and Artifact field
    populated and verify the long tail of columns lands in the database
    and comes back through both the JSON document and ADQL."""
    from ska_src_mm_notification.builder import NotificationBuilder
    from ska_src_mm_notification.models.schemas.srcnet_ingestion import Artifact, DataProduct

    artifact = Artifact(
        artifact_id="full-art-1",
        access_url="https://example.org/full.fits",
        access_format="application/fits",
        access_estsize=4096,
        path_to_parent="obs/full",
        semantics="science",
        s_ra=62.3,
        s_dec=-65.5,
        s_fov=1.5,
        s_region="CIRCLE ICRS 62.3 -65.5 1.5",
        em_wlen=0.21,
        em_min=0.20,
        em_max=0.22,
        t_min=60000.0,
        t_max=60001.0,
        t_exptime=3600.0,
        pol_states="XXYY",
        pol_xel=2,
    )
    product = DataProduct(
        product_id="full-prod-1",
        o_ucd="phot.flux",
        dataproduct_type="cube",
        calib_level=3,
        data_product_origin="ODP",
        target_name="Full Target",
        is_calibrator=True,
        calibrator_type="bandpass",
        em_band="Radio",
        s_ra=62.3,
        s_dec=-65.5,
        s_fov=1.5,
        s_region="CIRCLE ICRS 62.3 -65.5 1.5",
        em_wlen=0.21,
        em_min=0.20,
        em_max=0.22,
        t_min=60000.0,
        t_max=60001.0,
        t_exptime=3600.0,
        s_xel1=1024,
        s_xel2=1024,
        em_xel=16384,
        t_xel=1,
        baseline_min=29.0,
        baseline_max=74000.0,
        num_baselines=2016,
        num_antennas=64,
        beam_size=7.5,
        beam_maj=8.0,
        beam_min=7.0,
        beam_pa=12.5,
        pol_states="XXYY",
        pol_xel=2,
        baselines=[29, 74000],
        calibrator_targets=["J1939-6342"],
        artifacts=[artifact],
    )
    payload = (
        NotificationBuilder()
        .create_simple_notification(
            project_id="component-full",
            group_ids=["group-full"],
            obs_id="obs-full",
            obs_title="Fully populated observation",
            eb_id="eb-full",
            data_products=[product],
            project_title="Full metadata project",
            pi_name="Full PI",
            data_rights="public",
            instrument_name="MeerKAT",
            facility_name="SKAO",
        )
        .build_dict()
    )

    response = httpx.post(f"{_api(tap_service)}/notifications", json=payload, timeout=30)
    assert response.status_code == 201, response.text

    document = httpx.get(f"{_api(tap_service)}/notifications/component-full", timeout=10).json()
    stored_product = document["observations"][0]["scheduling_blocks"][0]["execution_blocks"][0][
        "data_products"
    ][0]
    for field, expected in (
        ("beam_pa", 12.5),
        ("num_antennas", 64),
        ("s_xel1", 1024),
        ("is_calibrator", True),
    ):
        assert stored_product[field] == expected, field
    (stored_artifact,) = stored_product["artifacts"]
    assert stored_artifact["t_exptime"] == 3600.0
    assert stored_artifact["pol_states"] == "XXYY"

    sync = httpx.post(
        f"{tap_service}/sync",
        data={
            "QUERY": (
                "SELECT p.beam_maj, p.num_baselines, a.em_min"
                " FROM srcnet.data_products AS p"
                " JOIN srcnet.artifacts AS a ON a.product_id = p.product_id"
                " WHERE p.project_id = 'component-full'"
            ),
            "LANG": "ADQL",
            "RESPONSEFORMAT": "csv",
        },
        timeout=30,
    )
    assert sync.status_code == 200
    assert "8.0,2016,0.2" in sync.text


def test_schema_evolution_adds_new_model_columns_without_data_loss(tap_service, database_url):
    """Simulate a library release that adds a field: an existing table
    missing a column is migrated forward by ensure_schema (ADD COLUMN IF
    NOT EXISTS) and already-ingested rows survive."""
    import psycopg
    from tap_api.plugins.odp import ensure_schema

    httpx.post(f"{_api(tap_service)}/notifications", json=SRC_INGESTION_EXAMPLE, timeout=30)

    with psycopg.connect(database_url, autocommit=True) as conn:
        before = conn.execute(
            "SELECT count(*) FROM srcnet.data_products WHERE project_id = 'project12314'"
        ).fetchone()[0]
        assert before >= 1
        # pretend this deployment predates the beam_pa field
        conn.execute("ALTER TABLE srcnet.data_products DROP COLUMN beam_pa")

    with psycopg.connect(database_url) as conn, conn.transaction():
        ensure_schema(conn)

    with psycopg.connect(database_url) as conn:
        restored = conn.execute(
            "SELECT beam_pa FROM srcnet.data_products WHERE project_id = 'project12314'"
        ).fetchall()
        assert len(restored) == before  # rows survived, column is back (NULL)
        assert all(value is None for (value,) in restored)


def test_amend_backfills_new_column_on_real_database(tap_service):
    """PATCH /notifications/{project}: backfill a column across rows and
    verify via the document and ADQL; constraint violations are rejected."""
    httpx.post(f"{_api(tap_service)}/notifications", json=SRC_INGESTION_EXAMPLE, timeout=30)
    url = f"{_api(tap_service)}/notifications/project12314"

    response = httpx.patch(
        url, json={"table": "data_products", "values": {"beam_pa": 42.0}}, timeout=30
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] >= 1

    sync = httpx.post(
        f"{tap_service}/sync",
        data={
            "QUERY": (
                "SELECT DISTINCT beam_pa FROM srcnet.data_products"
                " WHERE project_id = 'project12314'"
            ),
            "LANG": "ADQL",
            "RESPONSEFORMAT": "csv",
        },
        timeout=30,
    )
    assert sync.text.strip().splitlines()[1:] == ["42.0"]

    # the pydantic constraint (beam_pa in [-180, 180]) still guards amendments
    rejected = httpx.patch(
        url, json={"table": "data_products", "values": {"beam_pa": 999.0}}, timeout=30
    )
    assert rejected.status_code == 400
    # and the database CHECK would catch anything that slipped through
    scoped = httpx.patch(
        url,
        json={
            "table": "data_products",
            "match": {"product_id": "nope"},
            "values": {"beam_pa": 1.0},
        },
        timeout=30,
    )
    assert scoped.json()["updated"] == 0
