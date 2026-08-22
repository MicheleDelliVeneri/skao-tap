"""VOSI endpoints: availability, capabilities (TAPRegExt), tables (VODataService)."""

from xml.etree import ElementTree as ET

import httpx
import pytest
import pyvo

pytestmark = pytest.mark.component

NS = {
    "vosi-avail": "http://www.ivoa.net/xml/VOSIAvailability/v1.0",
    "vosi-caps": "http://www.ivoa.net/xml/VOSICapabilities/v1.0",
    "vosi-tables": "http://www.ivoa.net/xml/VOSITables/v1.0",
    "vod": "http://www.ivoa.net/xml/VODataService/v1.1",
}


def test_availability(tap_service):
    response = httpx.get(f"{tap_service}/availability")
    assert response.status_code == 200
    root = ET.fromstring(response.text)
    assert root.find("vosi-avail:available", NS).text == "true"


def test_capabilities_declare_tap_and_vosi(tap_service):
    root = ET.fromstring(httpx.get(f"{tap_service}/capabilities").text)
    standard_ids = {c.get("standardID") for c in root.findall("capability")}
    assert "ivo://ivoa.net/std/TAP" in standard_ids
    assert "ivo://ivoa.net/std/VOSI#capabilities" in standard_ids
    assert "ivo://ivoa.net/std/VOSI#availability" in standard_ids
    assert "ivo://ivoa.net/std/VOSI#tables" in standard_ids

    tap_cap = next(
        c for c in root.findall("capability") if c.get("standardID") == "ivo://ivoa.net/std/TAP"
    )
    languages = {lang.findtext("name") for lang in tap_cap.findall("language")}
    assert "ADQL" in languages
    mimes = {of.findtext("mime") for of in tap_cap.findall("outputFormat")}
    assert {"application/x-votable+xml", "text/csv", "text/tab-separated-values"} <= mimes


def test_capabilities_parse_with_pyvo(tap_service):
    svc = pyvo.dal.TAPService(tap_service)
    ids = {c.standardid for c in svc.capabilities}
    assert any(str(i).startswith("ivo://ivoa.net/std/TAP") for i in ids)


def test_tables_endpoint_lists_published_tables(tap_service):
    root = ET.fromstring(httpx.get(f"{tap_service}/tables").text)
    tables = {t.findtext("name") for t in root.iter("table")}
    assert "ska.continuum_sources" in tables
    assert "tap_schema.columns" in tables


def test_tables_metadata_via_pyvo(tap_service):
    svc = pyvo.dal.TAPService(tap_service)
    table = svc.tables["ska.continuum_sources"]
    columns = {c.name: c for c in table.columns}
    assert "ra" in columns and "dec" in columns
    assert columns["ra"].unit == "deg"
    assert columns["ra"].ucd == "pos.eq.ra;meta.main"
