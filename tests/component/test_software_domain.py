"""Component tests for the software discovery metadata plugin: ingest with
the ska-src-sdm model, TAP/ADQL queryability of the generated software
schema, document roundtrip with flattened nested objects, amendments, and deletion."""

import httpx
import pytest
from ska_src_sdm import Software

pytestmark = pytest.mark.component

PAYLOAD = {
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
            "supported_modes": ["HEADLESS", "NOTEBOOK"],
        },
        {
            "kind": "SINGULARITY",
            "location": "library://ska/dsc-037-delay-ps:0.1.3",
            "cpu_architecture": ["amd64"],
        },
    ],
    "discovery": {"science_category": ["EoR"], "tools_included": ["casa", "wsclean"]},
    "data_compatibility": {"data_input_type": ["visibility"], "data_output_type": ["spectrum"]},
    "resources": {"requires_gpu": True, "min_memory": 16, "recommended_memory": 64},
    "provenance": {
        "repository_url": "https://gitlab.com/ska/dsc-037",
        "registered_by": "onyx",
        "registration_date": "2026-01-16T00:00:00Z",
    },
}


def _api(tap_service: str) -> str:
    return tap_service.rsplit("/tap", 1)[0] + "/api/v1"


def test_software_ingest_built_with_the_model(tap_service):
    """Producer path: the payload validates through the ska-src-sdm model
    itself before being sent, then upserts idempotently."""
    document = Software.from_dict(PAYLOAD)  # raises on invalid payload
    response = httpx.post(f"{_api(tap_service)}/software", json=document.to_dict(), timeout=30)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["uri"] == PAYLOAD["uri"]
    assert body["rows"] == {"srcnet.software": 1, "srcnet.software_artifacts": 2}
    again = httpx.post(f"{_api(tap_service)}/software", json=PAYLOAD, timeout=30)
    assert again.status_code == 201
    assert again.json()["rows"] == body["rows"]


def test_software_document_roundtrip(tap_service):
    httpx.post(f"{_api(tap_service)}/software", json=PAYLOAD, timeout=30)
    document = httpx.get(f"{_api(tap_service)}/software/{PAYLOAD['uri']}", timeout=10).json()
    assert document["description"] == PAYLOAD["description"]
    # flattened nested objects come back nested
    assert document["resources"]["min_memory"] == 16
    assert document["provenance"]["registered_by"] == "onyx"
    assert document["discovery"]["tools_included"] == ["casa", "wsclean"]
    assert {a["kind"] for a in document["artifacts"]} == {"DOCKER", "SINGULARITY"}

    listing = httpx.get(f"{_api(tap_service)}/software", timeout=10).json()
    (entry,) = [s for s in listing["software"] if s["uri"] == PAYLOAD["uri"]]
    assert entry["artifacts"] == 2
    assert entry["status"] == "STABLE"


def test_software_queryable_via_tap_adql(tap_service):
    httpx.post(f"{_api(tap_service)}/software", json=PAYLOAD, timeout=30)
    sync = httpx.post(
        f"{tap_service}/sync",
        data={
            "QUERY": (
                "SELECT s.uri, s.status, a.location"
                " FROM srcnet.software AS s"
                " JOIN srcnet.software_artifacts AS a ON a.uri = s.uri"
                " WHERE a.kind = 'DOCKER'"
            ),
            "LANG": "ADQL",
            "RESPONSEFORMAT": "csv",
        },
        timeout=30,
    )
    assert sync.status_code == 200, sync.text
    assert "ska:dsc-037-delay-ps:0.1.3,STABLE,images.canfar.net" in sync.text


def test_software_amend_flattened_column(tap_service):
    httpx.post(f"{_api(tap_service)}/software", json=PAYLOAD, timeout=30)
    url = f"{_api(tap_service)}/software/{PAYLOAD['uri']}"
    amended = httpx.patch(
        url, json={"table": "software", "values": {"status": "DEPRECATED"}}, timeout=10
    )
    assert amended.status_code == 200, amended.text
    assert amended.json()["updated"] == 1
    assert httpx.get(url, timeout=10).json()["status"] == "DEPRECATED"
    # constraints hold through flattening: model says min_memory >= 1
    rejected = httpx.patch(
        url, json={"table": "software", "values": {"resources_min_memory": 0}}, timeout=10
    )
    assert rejected.status_code == 400
    # and the generated CHECK constraint guards the enum column
    bad_enum = httpx.patch(
        url, json={"table": "software", "values": {"status": "SHINY"}}, timeout=10
    )
    assert bad_enum.status_code == 400


def test_software_delete_cascades_to_artifacts(tap_service):
    payload = {**PAYLOAD, "uri": "ska:delete-demo:0.0.1"}
    url = f"{_api(tap_service)}/software/{payload['uri']}"
    created = httpx.post(f"{_api(tap_service)}/software", json=payload, timeout=30)
    assert created.status_code == 201, created.text

    deleted = httpx.delete(url, timeout=30)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted", "uri": payload["uri"]}
    fetched_after_delete = httpx.get(url, timeout=10)
    deleted_again = httpx.delete(url, timeout=10)
    assert fetched_after_delete.status_code == 404
    assert deleted_again.status_code == 404

    artifacts = httpx.post(
        f"{tap_service}/sync",
        data={
            "QUERY": (f"SELECT uri FROM srcnet.software_artifacts WHERE uri = '{payload['uri']}'"),
            "LANG": "ADQL",
            "RESPONSEFORMAT": "csv",
        },
        timeout=30,
    )
    assert artifacts.status_code == 200, artifacts.text
    assert artifacts.text.strip().splitlines() == ["uri"]


def test_software_validation_rejected(tap_service):
    bad = dict(PAYLOAD, uri="not a uri")
    response = httpx.post(f"{_api(tap_service)}/software", json=bad, timeout=10)
    assert response.status_code == 422
