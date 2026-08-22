"""Unit tests for result serialization."""

import datetime
import json
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest
from tapcore.votable import error_votable, normalize_format, serialize

NAMES = ["id", "name", "flux", "obs"]
ROWS = [
    (1, "alpha", Decimal("1.5"), datetime.datetime(2026, 1, 1)),
    (2, None, None, None),
]

VOT_NS = {"v": "http://www.ivoa.net/xml/VOTable/v1.3"}


def _parse(body: bytes):
    return ET.fromstring(body.decode())


def test_normalize_format_aliases_and_mimes():
    assert normalize_format(None) == ("votable", "application/x-votable+xml", "vot")
    assert normalize_format("votable")[1] == "application/x-votable+xml"
    assert normalize_format("text/csv") == ("csv", "text/csv", "csv")
    assert normalize_format("TSV")[0] == "tsv"
    assert normalize_format("application/json")[0] == "json"


def test_normalize_format_rejects_unknown():
    with pytest.raises(ValueError):
        normalize_format("fits")


def test_votable_structure_and_status():
    root = _parse(serialize(NAMES, ROWS, "votable", "OK"))
    infos = root.findall(".//v:INFO", VOT_NS)
    assert any(i.get("name") == "QUERY_STATUS" and i.get("value") == "OK" for i in infos)
    fields = [f.get("name") for f in root.findall(".//v:FIELD", VOT_NS)]
    assert fields == NAMES
    trs = root.findall(".//v:TR", VOT_NS)
    assert len(trs) == 2


def test_votable_overflow_status():
    root = _parse(serialize(NAMES, ROWS, "votable", "OVERFLOW"))
    infos = root.findall(".//v:INFO", VOT_NS)
    assert any(i.get("value") == "OVERFLOW" for i in infos)


def test_votable_empty_result():
    root = _parse(serialize(NAMES, [], "votable", "OK"))
    assert [f.get("name") for f in root.findall(".//v:FIELD", VOT_NS)] == NAMES
    assert root.findall(".//v:TR", VOT_NS) == []


def test_csv_and_tsv():
    csv_out = serialize(NAMES, ROWS, "csv").decode().splitlines()
    assert csv_out[0] == "id,name,flux,obs"
    assert csv_out[1] == "1,alpha,1.5,2026-01-01T00:00:00"
    assert csv_out[2] == "2,,,"
    tsv_out = serialize(NAMES, ROWS, "tsv").decode().splitlines()
    assert tsv_out[0] == "id\tname\tflux\tobs"


def test_json():
    payload = json.loads(serialize(NAMES, ROWS, "json"))
    assert payload["status"] == "OK"
    assert [c["name"] for c in payload["metadata"]] == NAMES
    assert payload["data"][0] == [1, "alpha", 1.5, "2026-01-01T00:00:00"]
    assert payload["data"][1] == [2, None, None, None]


def test_error_votable_is_dali_error_document():
    root = _parse(error_votable("something <bad> happened"))
    info = root.find(".//v:INFO", VOT_NS)
    assert info.get("name") == "QUERY_STATUS"
    assert info.get("value") == "ERROR"
    assert "something <bad> happened" in info.text
