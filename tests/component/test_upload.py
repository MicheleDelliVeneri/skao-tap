"""Component tests for TAP table upload (UPLOAD): inline multipart uploads
joined against published tables, sync and async, plus limit/error semantics."""

import time
from xml.etree import ElementTree as ET

import httpx
import pytest

pytestmark = pytest.mark.component

VOT = {"v": "http://www.ivoa.net/xml/VOTable/v1.3"}

UPLOAD_VOTABLE = b"""<?xml version="1.0"?>
<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">
<RESOURCE><TABLE>
<FIELD name="source_name" datatype="char" arraysize="*"/>
<FIELD name="weight" datatype="double"/>
<DATA><TABLEDATA>
<TR><TD>SKA-CS J1959+4044</TD><TD>2.0</TD></TR>
<TR><TD>does-not-exist</TD><TD>1.0</TD></TR>
</TABLEDATA></DATA>
</TABLE></RESOURCE></VOTABLE>"""

JOIN_QUERY = (
    "SELECT s.source_name, s.flux_int, u.weight"
    " FROM ska.continuum_sources AS s"
    " JOIN TAP_UPLOAD.mine AS u ON s.source_name = u.source_name"
)


def _rows(text: str) -> list[list[str | None]]:
    root = ET.fromstring(text)
    return [[td.text for td in tr.findall("v:TD", VOT)] for tr in root.iter(f"{{{VOT['v']}}}TR")]


def test_sync_inline_upload_join(tap_service):
    response = httpx.post(
        f"{tap_service}/sync",
        data={"QUERY": JOIN_QUERY, "LANG": "ADQL", "UPLOAD": "mine,param:mine"},
        files={"mine": ("mine.vot", UPLOAD_VOTABLE, "application/x-votable+xml")},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    rows = _rows(response.text)
    assert len(rows) == 1  # only the real source joins
    assert rows[0][0] == "SKA-CS J1959+4044"
    assert float(rows[0][2]) == 2.0


def test_sync_upload_only_query(tap_service):
    response = httpx.post(
        f"{tap_service}/sync",
        data={
            "QUERY": "SELECT u.source_name FROM TAP_UPLOAD.mine AS u",
            "LANG": "ADQL",
            "UPLOAD": "mine,param:mine",
            "RESPONSEFORMAT": "csv",
        },
        files={"mine": ("mine.vot", UPLOAD_VOTABLE, "application/x-votable+xml")},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    lines = response.text.strip().splitlines()
    assert lines[0] == "source_name"
    assert set(lines[1:]) == {"SKA-CS J1959+4044", "does-not-exist"}


def test_sync_unreferenced_upload_is_rejected(tap_service):
    response = httpx.post(
        f"{tap_service}/sync",
        data={
            "QUERY": "SELECT u.x FROM TAP_UPLOAD.other AS u",
            "LANG": "ADQL",
            "UPLOAD": "mine,param:mine",
        },
        files={"mine": ("mine.vot", UPLOAD_VOTABLE, "application/x-votable+xml")},
        timeout=30,
    )
    assert response.status_code == 400
    assert "was not uploaded" in response.text


def test_async_upload_join(tap_service):
    created = httpx.post(
        f"{tap_service}/async",
        data={
            "QUERY": JOIN_QUERY,
            "LANG": "ADQL",
            "UPLOAD": "mine,param:mine",
            "PHASE": "RUN",
        },
        files={"mine": ("mine.vot", UPLOAD_VOTABLE, "application/x-votable+xml")},
        timeout=30,
        follow_redirects=False,
    )
    assert created.status_code == 303
    job_url = created.headers["location"]
    for _ in range(60):
        phase = httpx.get(f"{job_url}/phase", timeout=10).text
        if phase in ("COMPLETED", "ERROR", "ABORTED"):
            break
        time.sleep(0.5)
    assert phase == "COMPLETED", httpx.get(f"{job_url}/error", timeout=10).text
    result = httpx.get(f"{job_url}/results/result", timeout=30)
    assert result.status_code == 200
    rows = _rows(result.text)
    assert len(rows) == 1
    assert rows[0][0] == "SKA-CS J1959+4044"


def test_capabilities_declare_upload(tap_service):
    text = httpx.get(f"{tap_service}/capabilities", timeout=10).text
    assert "ivo://ivoa.net/std/TAPRegExt#upload-inline" in text
    assert "<uploadLimit>" in text
