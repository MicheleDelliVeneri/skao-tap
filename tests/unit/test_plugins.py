"""Unit tests for the metadata plugin system (tapcore.plugins) and the
built-in software discovery domain."""

import datetime

import pytest
from tapcore.config import settings
from tapcore.plugins import active_plugins, discovered_plugins
from tapcore.schema_gen import build_tables

SOFTWARE_PAYLOAD = {
    "uri": "ska:dsc-037-delay-ps:0.1.3",
    "description": "Delay power-spectrum pipeline",
    "release_date": "2026-01-15T00:00:00Z",
    "status": "STABLE",
    "artifacts": [
        {
            "kind": "DOCKER",
            "location": "images.canfar.net/dsc-037-delay-ps:0.1.3",
            "cpu_architecture": ["amd64", "arm64"],
            "digest": "sha256:" + "ab" * 32,
            "supported_modes": ["HEADLESS"],
        }
    ],
    "discovery": {"science_category": ["EoR"], "tools_included": ["casa"]},
    "resources": {"requires_gpu": True, "min_memory": 16},
    "provenance": {"registered_by": "onyx", "registration_date": "2026-01-16T00:00:00Z"},
}


@pytest.fixture
def plugin_selection():
    """Temporarily set TAP_MODEL_PLUGINS (Settings is frozen)."""
    original = settings.model_plugins

    def select(value: str):
        object.__setattr__(settings, "model_plugins", value)

    yield select
    object.__setattr__(settings, "model_plugins", original)


def test_builtin_plugins_are_discovered_via_entry_points():
    plugins = discovered_plugins()
    assert plugins["odp"].sql_schema == "srcnet"
    assert plugins["odp"].mount == "notifications"
    assert plugins["software"].sql_schema == "software"


def test_selection_all_and_subset(plugin_selection):
    plugin_selection("all")
    assert {p.name for p in active_plugins()} == {"odp", "software"}
    plugin_selection("software")
    assert [p.name for p in active_plugins()] == ["software"]
    plugin_selection("odp, software")
    assert [p.name for p in active_plugins()] == ["odp", "software"]
    plugin_selection("cheese")
    with pytest.raises(LookupError, match="unknown plugin 'cheese'"):
        active_plugins()


def test_identity_overrides_and_flattening():
    from ska_src_sdm import Software

    tables = build_tables(
        Software, "software", "software", {"Software": "uri", "Artifact": "location"}
    )
    root, artifacts = tables
    assert root.pk_columns == ["uri"]
    assert artifacts.pk_columns == ["uri", "location"]
    by_name = {c.name: c for c in root.columns}
    assert by_name["release_date"].sql_type == "timestamptz"
    assert by_name["resources_min_memory"].sql_type == "bigint"
    assert by_name["resources_min_memory"].path == ("resources", "min_memory")
    assert by_name["resources_min_memory"].checks == ["resources_min_memory >= 1"]
    assert by_name["provenance_registration_date"].sql_type == "timestamptz"
    # column -> pydantic field resolution through the flattening
    info = root.field_for_column("resources_min_memory")
    assert info is not None and info.metadata


def test_identity_requires_override_or_id_field():
    from pydantic import BaseModel

    class NoId(BaseModel):
        title: str

    with pytest.raises(ValueError, match="identity"):
        build_tables(NoId, "x", "things")
    with pytest.raises(ValueError, match="identity override"):
        build_tables(NoId, "x", "things", {"NoId": "nope"})


def test_software_ingest_list_fetch_amend(client, fake_db):
    response = client.post("/api/v1/software", json=SOFTWARE_PAYLOAD)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "ingested"
    assert body["uri"] == SOFTWARE_PAYLOAD["uri"]
    assert body["rows"] == {"software.software": 1, "software.artifacts": 1}

    listing = client.get("/api/v1/software").json()
    (entry,) = listing["software"]
    assert entry["uri"] == SOFTWARE_PAYLOAD["uri"]
    assert entry["artifacts"] == 1
    # flattened columns come back as the nested object
    assert entry["resources"]["min_memory"] == 16

    document = client.get(f"/api/v1/software/{SOFTWARE_PAYLOAD['uri']}").json()
    assert document["description"] == SOFTWARE_PAYLOAD["description"]
    assert document["resources"] == {"requires_gpu": True, "min_memory": 16}
    assert document["provenance"]["registered_by"] == "onyx"
    (artifact,) = document["artifacts"]
    assert artifact["kind"] == "DOCKER"
    assert artifact["cpu_architecture"] == ["amd64", "arm64"]

    amended = client.patch(
        f"/api/v1/software/{SOFTWARE_PAYLOAD['uri']}",
        json={"table": "software", "values": {"resources_min_memory": 32}},
    )
    assert amended.status_code == 200, amended.text
    assert amended.json()["updated"] == 1
    # the flattened column still enforces the model's Ge(1) constraint
    rejected = client.patch(
        f"/api/v1/software/{SOFTWARE_PAYLOAD['uri']}",
        json={"table": "software", "values": {"resources_min_memory": 0}},
    )
    assert rejected.status_code == 400
    assert "resources_min_memory" in rejected.json()["message"]


def test_software_validation_rejects_bad_payload(client):
    bad = dict(SOFTWARE_PAYLOAD, uri="not-a-valid-uri")
    assert client.post("/api/v1/software", json=bad).status_code == 422
    missing = {k: v for k, v in SOFTWARE_PAYLOAD.items() if k != "artifacts"}
    assert client.post("/api/v1/software", json=missing).status_code == 422


def test_unknown_software_document_is_404(client):
    assert client.get("/api/v1/software/ska:nope:0.0.1").status_code == 404


def test_ingested_datetime_roundtrip(client, fake_db):
    client.post("/api/v1/software", json=SOFTWARE_PAYLOAD)
    stored = next(iter(fake_db.srcnet["software.software"].values()))
    assert isinstance(stored["release_date"], datetime.datetime)
    assert stored["release_date"].year == 2026
