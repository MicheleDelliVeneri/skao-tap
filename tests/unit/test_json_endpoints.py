"""Unit tests for the /api/v1 JSON facade (egernia_api.endpoints.json_api) on the fake pool."""

import json
import os

from ska_src_mm_notification.models.schemas.srcnet_ingestion import SRC_INGESTION_EXAMPLE

QUERY = "SELECT source_id, ra FROM ska.continuum_sources"


def test_sync_query_json(client):
    response = client.post("/api/v1/query", json={"query": QUERY})
    assert response.status_code == 200
    body = response.json()
    assert [c["name"] for c in body["metadata"]] == ["source_id", "ra"]
    assert body["data"] == [[1, 62.1], [2, 62.2]]
    assert body["status"] == "OK"


def test_sync_query_validation(client):
    missing_body = client.post("/api/v1/query", json={})
    assert missing_body.status_code == 422
    bad_lang = client.post("/api/v1/query", json={"query": QUERY, "lang": "SQL"})
    assert bad_lang.status_code == 400


def test_create_job_pending(client, fake_db):
    response = client.post("/api/v1/jobs", json={"query": QUERY, "run_id": "batch-1"})
    assert response.status_code == 201
    body = response.json()
    assert body["phase"] == "PENDING"
    assert body["run_id"] == "batch-1"
    assert body["parameters"]["QUERY"] == QUERY
    assert body["urls"]["job"].endswith(f"/api/v1/jobs/{body['job_id']}")
    assert fake_db.jobs[body["job_id"]]["phase"] == "PENDING"


def test_create_job_run_immediately(client, fake_db):
    response = client.post("/api/v1/jobs", json={"query": QUERY, "run": True})
    body = response.json()
    assert body["phase"] == "QUEUED"
    stored = fake_db.jobs[body["job_id"]]
    assert "ska.continuum_sources" in stored["query_sql"]
    # from the submit-time parse, so the executor never parses the SQL again
    assert stored["query_tables"] == ["ska.continuum_sources"]


def test_create_job_validates_before_storing(client, fake_db):
    response = client.post("/api/v1/jobs", json={"query": "SELECT x FROM private.hidden"})
    assert response.status_code == 400
    assert fake_db.jobs == {}


def test_list_jobs_and_filters(client, fake_db):
    client.post("/api/v1/jobs", json={"query": QUERY})
    fake_db.add_job(phase="EXECUTING")
    assert len(client.get("/api/v1/jobs").json()["jobs"]) == 2
    filtered = client.get("/api/v1/jobs", params={"phase": "executing"}).json()["jobs"]
    assert [j["phase"] for j in filtered] == ["EXECUTING"]
    assert client.get("/api/v1/jobs", params={"phase": "NAPPING"}).status_code == 400
    assert client.get("/api/v1/jobs", params={"last": 0}).status_code == 400


def test_get_job_completed_and_error_bodies(client, fake_db):
    done = fake_db.add_job(phase="COMPLETED", result_mime="text/csv", result_size=42)
    body = client.get(f"/api/v1/jobs/{done['job_id']}").json()
    assert body["result"]["mime"] == "text/csv"
    assert body["result"]["size"] == 42
    failed = fake_db.add_job(phase="ERROR", error_type="fatal", error_message="boom")
    body = client.get(f"/api/v1/jobs/{failed['job_id']}").json()
    assert body["error"] == {"type": "fatal", "message": "boom"}
    assert client.get("/api/v1/jobs/0123456789abcdef").status_code == 404


def test_phase_transitions(client, fake_db):
    job_id = client.post("/api/v1/jobs", json={"query": QUERY}).json()["job_id"]
    run = client.post(f"/api/v1/jobs/{job_id}/phase", json={"phase": "run"})
    assert run.json()["phase"] == "QUEUED"
    abort = client.post(f"/api/v1/jobs/{job_id}/phase", json={"phase": "ABORT"})
    assert abort.json()["phase"] == "ABORTED"
    again = client.post(f"/api/v1/jobs/{job_id}/phase", json={"phase": "ABORT"})
    assert again.json()["phase"] == "ABORTED"
    unknown = client.post(f"/api/v1/jobs/{job_id}/phase", json={"phase": "PAUSE"})
    assert unknown.status_code == 400
    rerun = client.post(f"/api/v1/jobs/{job_id}/phase", json={"phase": "RUN"})
    assert rerun.status_code == 400


def test_delete_job(client, fake_db, caplog):
    job_id = client.post("/api/v1/jobs", json={"query": QUERY}).json()["job_id"]
    deleted = client.delete(f"/api/v1/jobs/{job_id}")
    assert deleted.status_code == 204
    assert job_id not in fake_db.jobs
    assert "failed to remove the result files" not in caplog.text
    missing = client.delete(f"/api/v1/jobs/{job_id}")
    assert missing.status_code == 404


def test_delete_job_warns_on_unexpected_cleanup_error(client, monkeypatch, caplog):
    from egernia_api.endpoints import json_api

    job_id = client.post("/api/v1/jobs", json={"query": QUERY}).json()["job_id"]

    def fail_cleanup(_path):
        raise PermissionError

    monkeypatch.setattr(json_api.shutil, "rmtree", fail_cleanup)
    deleted = client.delete(f"/api/v1/jobs/{job_id}")
    assert deleted.status_code == 204
    # the id is a request value (py/log-injection), so it stays out of the
    # message; the traceback is what makes the record actionable
    assert "failed to remove the result files of a deleted job" in caplog.text
    assert "PermissionError" in caplog.text
    assert job_id not in caplog.text


def test_job_result_download(client, fake_db, results_dir):
    job = fake_db.add_job(phase="COMPLETED", result_mime="application/json")
    job_id = job["job_id"]
    os.makedirs(os.path.join(results_dir, job_id))
    with open(os.path.join(results_dir, job_id, "result.json"), "wb") as fh:
        fh.write(b'{"data": []}')
    response = client.get(f"/api/v1/jobs/{job_id}/result")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    pending = fake_db.add_job(phase="PENDING")
    assert client.get(f"/api/v1/jobs/{pending['job_id']}/result").status_code == 404
    empty = fake_db.add_job(phase="COMPLETED")
    assert client.get(f"/api/v1/jobs/{empty['job_id']}/result").status_code == 404


def test_tables_json(client):
    body = client.get("/api/v1/tables").json()
    assert body["tables"][0]["name"] == "ska.continuum_sources"
    assert body["tables"][0]["columns"][0]["name"] == "source_id"


def test_notification_ingest_list_and_roundtrip(client, fake_db):
    response = client.post("/api/v1/notifications", json=SRC_INGESTION_EXAMPLE)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ingested"
    assert body["project_id"] == SRC_INGESTION_EXAMPLE["project_id"]
    assert sum(body["rows"].values()) >= 1
    # idempotent upsert: same counts on re-post
    again = client.post("/api/v1/notifications", json=SRC_INGESTION_EXAMPLE)
    assert again.status_code == 201
    assert again.json()["rows"] == body["rows"]

    listing = client.get("/api/v1/notifications").json()
    (project,) = listing["projects"]
    assert project["project_id"] == SRC_INGESTION_EXAMPLE["project_id"]
    assert project["data_products"] == body["rows"].get("srcnet.data_products", 0)

    document = client.get(f"/api/v1/notifications/{body['project_id']}").json()
    assert document["project_title"] == SRC_INGESTION_EXAMPLE["project_title"]
    assert len(document["observations"]) == len(SRC_INGESTION_EXAMPLE["observations"])


def test_ingest_document_batches_one_statement_per_table():
    """A document costs one round trip per generated table, not one per row,
    and parent tables are written before their children so the FKs hold."""
    from egernia_api.plugins.odp import PLUGIN
    from egernia_core.metadata import ingest

    class Cursor:
        def __init__(self, calls):
            self._calls = calls

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def executemany(self, sql, rows):
            self._calls.append((sql, list(rows)))

    class Conn:
        def __init__(self):
            self.calls = []

        def cursor(self):
            return Cursor(self.calls)

    document = PLUGIN.model.model_validate(SRC_INGESTION_EXAMPLE)
    conn = Conn()
    counts = ingest.ingest_document(conn, PLUGIN, document)

    written = [sql.split()[2] for sql, _ in conn.calls]
    assert len(written) == len(set(written))
    assert sum(len(rows) for _, rows in conn.calls) == sum(counts.values())
    hierarchy = [t.qualified for t in PLUGIN.tables]
    assert written == [t for t in hierarchy if t in written]
    assert counts[PLUGIN.tables[0].qualified] == 1


def test_notification_validation_and_missing(client):
    bad = dict(SRC_INGESTION_EXAMPLE)
    bad.pop("project_id")
    invalid = client.post("/api/v1/notifications", json=bad)
    assert invalid.status_code == 422
    assert client.get("/api/v1/notifications/nope").status_code == 404


def _amend(client, project_id, **body):
    return client.patch(f"/api/v1/notifications/{project_id}", json=body)


def test_amend_backfills_a_column_across_rows(client):
    project_id = SRC_INGESTION_EXAMPLE["project_id"]
    client.post("/api/v1/notifications", json=SRC_INGESTION_EXAMPLE)
    response = _amend(client, project_id, table="data_products", values={"beam_pa": 12.5})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "amended"
    assert body["updated"] >= 1
    document = client.get(f"/api/v1/notifications/{project_id}").json()
    products = document["observations"][0]["scheduling_blocks"][0]["execution_blocks"][0][
        "data_products"
    ]
    assert all(p["beam_pa"] == 12.5 for p in products)


def test_amend_with_match_targets_specific_rows(client):
    project_id = SRC_INGESTION_EXAMPLE["project_id"]
    client.post("/api/v1/notifications", json=SRC_INGESTION_EXAMPLE)
    example_product = SRC_INGESTION_EXAMPLE["observations"][0]["scheduling_blocks"][0][
        "execution_blocks"
    ][0]["data_products"][0]
    response = _amend(
        client,
        project_id,
        table="data_products",
        match={"product_id": example_product["product_id"]},
        values={"num_antennas": 64},
    )
    assert response.json()["updated"] == 1
    missed = _amend(
        client,
        project_id,
        table="data_products",
        match={"product_id": "no-such-product"},
        values={"num_antennas": 64},
    )
    assert missed.json()["updated"] == 0


def test_amend_validates_against_the_model(client):
    project_id = SRC_INGESTION_EXAMPLE["project_id"]
    client.post("/api/v1/notifications", json=SRC_INGESTION_EXAMPLE)
    # beam_pa is constrained to [-180, 180] by the pydantic model
    bad_value = _amend(client, project_id, table="data_products", values={"beam_pa": 999.0})
    assert bad_value.status_code == 400
    assert "beam_pa" in bad_value.json()["message"]
    bad_column = _amend(client, project_id, table="data_products", values={"nope": 1})
    assert bad_column.status_code == 400
    bad_table = _amend(client, project_id, table="starships", values={"beam_pa": 1.0})
    assert bad_table.status_code == 400
    key_column = _amend(client, project_id, table="data_products", values={"product_id": "hijack"})
    assert key_column.status_code == 400
    assert "key column" in key_column.json()["message"]
    empty = _amend(client, project_id, table="data_products", values={})
    assert empty.status_code == 400


def test_amend_unknown_project_is_404(client):
    response = _amend(client, "ghost", table="data_products", values={"beam_pa": 1.0})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Region footprints: the derived pgsphere column (package 7)
# ---------------------------------------------------------------------------


def _stored_product(fake_db):
    (row,) = fake_db.srcnet["srcnet.data_products"].values()
    return row


def test_ingest_derives_the_geometry_from_s_region(client, fake_db):
    client.post("/api/v1/notifications", json=SRC_INGESTION_EXAMPLE)
    row = _stored_product(fake_db)
    assert row["s_region"] == "CIRCLE 3.5867 -30.4000 0.25"
    geom = row["s_region_geom"]
    assert geom.startswith("{(") and geom.endswith("d)}")
    assert geom.count("),(") == 31  # a 32-gon approximating the circle


def test_fetched_documents_do_not_carry_the_derived_column(client, fake_db):
    client.post("/api/v1/notifications", json=SRC_INGESTION_EXAMPLE)
    project_id = SRC_INGESTION_EXAMPLE["project_id"]
    document = client.get(f"/api/v1/notifications/{project_id}").json()
    product = document["observations"][0]["scheduling_blocks"][0]["execution_blocks"][0][
        "data_products"
    ][0]
    assert product["s_region"] == "CIRCLE 3.5867 -30.4000 0.25"
    assert "s_region_geom" not in product


def test_amending_s_region_rederives_the_geometry(client, fake_db):
    client.post("/api/v1/notifications", json=SRC_INGESTION_EXAMPLE)
    before = _stored_product(fake_db)["s_region_geom"]
    response = _amend(
        client,
        SRC_INGESTION_EXAMPLE["project_id"],
        table="data_products",
        values={"s_region": "CIRCLE 200.0 45.0 1.0"},
    )
    assert response.status_code == 200, response.text
    row = _stored_product(fake_db)
    assert row["s_region"] == "CIRCLE 200.0 45.0 1.0"
    assert row["s_region_geom"] != before


def test_amending_the_derived_column_directly_is_refused(client, fake_db):
    client.post("/api/v1/notifications", json=SRC_INGESTION_EXAMPLE)
    response = _amend(
        client,
        SRC_INGESTION_EXAMPLE["project_id"],
        table="data_products",
        values={"s_region_geom": "{(1d,2d),(3d,4d),(5d,6d)}"},
    )
    assert response.status_code == 400
    assert "derived from s_region" in response.text


def test_amending_s_region_with_garbage_is_refused(client, fake_db):
    client.post("/api/v1/notifications", json=SRC_INGESTION_EXAMPLE)
    response = _amend(
        client,
        SRC_INGESTION_EXAMPLE["project_id"],
        table="data_products",
        values={"s_region": "NOT A REGION"},
    )
    assert response.status_code == 400


def test_ingesting_a_malformed_region_is_rejected_by_the_model(client):
    bad = json.loads(json.dumps(SRC_INGESTION_EXAMPLE))
    bad["observations"][0]["scheduling_blocks"][0]["execution_blocks"][0]["data_products"][0][
        "s_region"
    ] = "CIRCLE 400.0 20.0 1.0"  # RA out of range: 0.1.8's validator rejects it
    response = client.post("/api/v1/notifications", json=bad)
    assert response.status_code == 422
