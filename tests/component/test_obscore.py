"""ObsCore 1.1 end to end (package 12): the odp bootstrap derives the
ivoa.obscore view, TAP publishes and serves it, and the declarations a
validator reads (capabilities, /tables) say so."""

import copy

import httpx
import pytest
from ska_src_mm_notification.models.schemas.srcnet_ingestion import (
    SRC_INGESTION_EXAMPLE,
)

pytestmark = pytest.mark.component


def _sync_csv(tap_service: str, adql: str) -> list[str]:
    response = httpx.post(
        f"{tap_service}/sync",
        data={"LANG": "ADQL", "QUERY": adql, "RESPONSEFORMAT": "csv"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    return response.text.strip().splitlines()


def test_obscore_view_serves_ingested_products(tap_service, api_url):
    payload = copy.deepcopy(SRC_INGESTION_EXAMPLE)
    payload["project_id"] = "obscore-demo"
    product = payload["observations"][0]["scheduling_blocks"][0]["execution_blocks"][0][
        "data_products"
    ][0]
    product["s_region"] = "CIRCLE ICRS 150.0 -30.0 0.5"
    created = httpx.post(f"{api_url}/notifications", json=payload, timeout=30)
    assert created.status_code == 201, created.text

    obs_id = payload["observations"][0]["obs_id"]

    # the REC's name is case-insensitive: ivoa.ObsCore works unquoted
    lines = _sync_csv(
        tap_service,
        "SELECT obs_publisher_did, obs_collection, dataproduct_type, calib_level"
        f" FROM ivoa.ObsCore WHERE obs_id = '{obs_id}'",
    )
    header, rows = lines[0].split(","), lines[1:]
    assert header == ["obs_publisher_did", "obs_collection", "dataproduct_type", "calib_level"]
    assert any(row.startswith(f"ivo://skao.int/~?obscore-demo/{obs_id}/") for row in rows)

    # the geometry companion answers footprint queries on the view itself
    overlap = _sync_csv(
        tap_service,
        "SELECT obs_publisher_did FROM ivoa.obscore"
        " WHERE 1=INTERSECTS(s_region_geom, CIRCLE('ICRS', 150.2, -30.0, 0.1))",
    )
    assert any("obscore-demo" in row for row in overlap[1:])


def test_obscore_declarations_for_validators(tap_service, api_url):
    capabilities = httpx.get(f"{tap_service}/capabilities", timeout=10).text
    assert (
        '<dataModel ivo-id="ivo://ivoa.net/std/ObsCore#core-1.1">ObsCore-1.1</dataModel>'
        in capabilities
    )

    tables = httpx.get(f"{tap_service}/tables", timeout=10).text
    assert "<name>ivoa.obscore</name>" in tables
    assert "<utype>ivo://ivoa.net/std/ObsCore#core-1.1</utype>" in tables
    assert 'extendedType="adql:REGION"' in tables
    assert "<utype>obscore:Curation.publisherDID</utype>" in tables

    listing = httpx.get(f"{api_url}/tables", timeout=10).json()
    obscore = next(t for t in listing["tables"] if t["name"] == "ivoa.obscore")
    assert obscore["utype"] == "ivo://ivoa.net/std/ObsCore#core-1.1"
    names = [c["name"] for c in obscore["columns"]]
    assert names[:5] == [
        "dataproduct_type",
        "calib_level",
        "obs_collection",
        "obs_id",
        "obs_publisher_did",
    ]
    assert "s_region_geom" in names

    examples = httpx.get(f"{tap_service}/examples", timeout=10).text
    assert "ivoa.obscore" in examples


def test_obscore_publisher_did_percent_encodes_the_key_chain(tap_service, api_url):
    """The key columns are free text and a PublisherDID is permanent: a
    product_id carrying a space, a '/' and a '#' must not forge a sixth path
    segment or truncate the identifier at the fragment."""
    payload = copy.deepcopy(SRC_INGESTION_EXAMPLE)
    payload["project_id"] = "obscore-escape"
    product = payload["observations"][0]["scheduling_blocks"][0]["execution_blocks"][0][
        "data_products"
    ][0]
    product["product_id"] = "cube 3/a#1"
    created = httpx.post(f"{api_url}/notifications", json=payload, timeout=30)
    assert created.status_code == 201, created.text

    lines = _sync_csv(
        tap_service,
        "SELECT obs_publisher_did FROM ivoa.obscore"
        " WHERE obs_publisher_did LIKE '%obscore-escape%'",
    )
    dids = lines[1:]
    assert dids, lines
    for did in dids:
        assert did.startswith("ivo://skao.int/~?obscore-escape/")
        assert did.endswith("/cube%203%2Fa%231")
        # the raw characters never reach the identifier
        assert " " not in did and "#" not in did
