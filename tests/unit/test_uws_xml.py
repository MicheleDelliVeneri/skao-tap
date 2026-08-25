"""Unit tests for UWS 1.1 job XML rendering."""

import datetime
from xml.etree import ElementTree as ET

from egernia_core import uws

NS = {
    "uws": "http://www.ivoa.net/xml/UWS/v1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}


def _job(**overrides):
    base = {
        "job_id": "abc123",
        "phase": "PENDING",
        "run_id": None,
        "owner_id": None,
        "quote": None,
        "creation_time": datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        "start_time": None,
        "end_time": None,
        "execution_duration": 600,
        "destruction": datetime.datetime(2026, 1, 8, tzinfo=datetime.UTC),
        "parameters": {"LANG": "ADQL", "QUERY": "SELECT 1"},
        "query_sql": None,
        "error_type": None,
        "error_message": None,
        "result_mime": None,
        "result_size": None,
    }
    base.update(overrides)
    return base


def test_job_xml_basic_fields():
    root = ET.fromstring(uws.job_xml(_job()).decode())
    assert root.tag == f"{{{NS['uws']}}}job"
    assert root.get("version") == "1.1"
    assert root.find("uws:jobId", NS).text == "abc123"
    assert root.find("uws:phase", NS).text == "PENDING"
    assert root.find("uws:creationTime", NS).text == "2026-01-01T00:00:00Z"
    assert root.find("uws:executionDuration", NS).text == "600"


def test_job_xml_nillable_fields():
    root = ET.fromstring(uws.job_xml(_job()).decode())
    for tag in ("runId", "ownerId", "quote", "startTime", "endTime"):
        el = root.find(f"uws:{tag}", NS)
        assert el.get(f"{{{NS['xsi']}}}nil") == "true", tag


def test_job_xml_parameters():
    root = ET.fromstring(uws.job_xml(_job()).decode())
    params = {p.get("id"): p.text for p in root.findall("uws:parameters/uws:parameter", NS)}
    assert params == {"lang": "ADQL", "query": "SELECT 1"}


def test_completed_job_has_result_reference():
    job = _job(phase="COMPLETED", result_mime="text/csv")
    root = ET.fromstring(uws.job_xml(job).decode())
    result = root.find("uws:results/uws:result", NS)
    assert result.get("id") == "result"
    assert result.get(f"{{{NS['xlink']}}}href").endswith("/async/abc123/results/result")
    assert result.get("mime-type") == "text/csv"


def test_error_job_has_error_summary():
    job = _job(phase="ERROR", error_type="fatal", error_message="division by zero")
    root = ET.fromstring(uws.job_xml(job).decode())
    err = root.find("uws:errorSummary", NS)
    assert err.get("type") == "fatal"
    assert err.find("uws:message", NS).text == "division by zero"


def test_joblist_xml():
    jobs = [_job(), _job(job_id="def456", phase="COMPLETED")]
    root = ET.fromstring(uws.joblist_xml(jobs).decode())
    refs = root.findall("uws:jobref", NS)
    assert [r.get("id") for r in refs] == ["abc123", "def456"]
    assert refs[1].find("uws:phase", NS).text == "COMPLETED"
    assert refs[0].get(f"{{{NS['xlink']}}}href").endswith("/async/abc123")
