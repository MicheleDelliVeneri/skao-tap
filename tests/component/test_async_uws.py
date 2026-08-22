"""Asynchronous queries: the UWS 1.1 job lifecycle over TAP /async,
exercised with PyVO (a standard IVOA client) plus raw HTTP for the
resources PyVO does not touch."""

from xml.etree import ElementTree as ET

import httpx
import pytest
import pyvo

pytestmark = pytest.mark.component

UWS = {"uws": "http://www.ivoa.net/xml/UWS/v1.0"}
QUERY = "SELECT source_id, source_name FROM ska.continuum_sources"


def _service(tap_service) -> pyvo.dal.TAPService:
    return pyvo.dal.TAPService(tap_service)


def test_full_job_lifecycle_with_pyvo(tap_service):
    svc = _service(tap_service)
    job = svc.submit_job(QUERY)
    assert job.phase == "PENDING"

    job.run()
    job.wait(phases=["COMPLETED", "ERROR", "ABORTED"], timeout=60)
    assert job.phase == "COMPLETED"

    table = job.fetch_result().to_table()
    assert len(table) == 8
    assert "source_name" in table.colnames

    job_url = job.url
    job.delete()
    assert httpx.get(job_url).status_code == 404


def test_job_created_with_phase_run_executes(tap_service):
    response = httpx.post(
        f"{tap_service}/async",
        data={"LANG": "ADQL", "QUERY": QUERY, "PHASE": "RUN"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_url = response.headers["location"]
    phase = pyvo.dal.AsyncTAPJob(job_url)
    phase.wait(phases=["COMPLETED", "ERROR", "ABORTED"], timeout=60)
    assert phase.phase == "COMPLETED"
    httpx.delete(job_url)


def test_job_summary_document(tap_service):
    svc = _service(tap_service)
    job = svc.submit_job(QUERY)
    root = ET.fromstring(httpx.get(job.url).text)
    assert root.tag == f"{{{UWS['uws']}}}job"
    assert root.find("uws:jobId", UWS).text == job.job_id
    assert root.find("uws:phase", UWS).text == "PENDING"
    params = {p.get("id").upper() for p in root.findall("uws:parameters/uws:parameter", UWS)}
    assert {"LANG", "QUERY"} <= params
    job.delete()


def test_executionduration_and_destruction_are_settable(tap_service):
    svc = _service(tap_service)
    job = svc.submit_job(QUERY)

    assert httpx.get(f"{job.url}/executionduration").text == "600"
    response = httpx.post(
        f"{job.url}/executionduration",
        data={"EXECUTIONDURATION": "300"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert httpx.get(f"{job.url}/executionduration").text == "300"

    response = httpx.post(
        f"{job.url}/destruction",
        data={"DESTRUCTION": "2030-01-01T00:00:00Z"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert httpx.get(f"{job.url}/destruction").text == "2030-01-01T00:00:00Z"

    # quote and owner resources exist
    assert httpx.get(f"{job.url}/quote").status_code == 200
    assert httpx.get(f"{job.url}/owner").status_code == 200
    job.delete()


def test_parameters_updatable_while_pending(tap_service):
    svc = _service(tap_service)
    job = svc.submit_job(QUERY)
    response = httpx.post(
        f"{job.url}/parameters",
        data={"QUERY": "SELECT TOP 2 source_id FROM ska.continuum_sources"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job.run()
    job.wait(phases=["COMPLETED", "ERROR", "ABORTED"], timeout=60)
    assert job.phase == "COMPLETED"
    assert len(job.fetch_result().to_table()) == 2
    job.delete()


def test_abort_job(tap_service):
    svc = _service(tap_service)
    job = svc.submit_job(QUERY)
    job.abort()
    assert job.phase == "ABORTED"
    job.delete()


def test_runtime_error_yields_error_phase_and_document(tap_service):
    svc = _service(tap_service)
    job = svc.submit_job("SELECT 1/0 AS boom FROM tap_schema.schemas")
    job.run()
    job.wait(phases=["COMPLETED", "ERROR", "ABORTED"], timeout=60)
    assert job.phase == "ERROR"

    error_doc = httpx.get(f"{job.url}/error")
    assert error_doc.status_code == 200
    assert 'value="ERROR"' in error_doc.text
    assert "division by zero" in error_doc.text

    summary = ET.fromstring(httpx.get(job.url).text).find("uws:errorSummary", UWS)
    assert summary is not None
    assert "division by zero" in summary.find("uws:message", UWS).text
    job.delete()


def test_invalid_query_rejected_at_run(tap_service):
    svc = _service(tap_service)
    job = svc.submit_job("SELECT * FROM uws.jobs")  # unpublished table
    response = httpx.post(f"{job.url}/phase", data={"PHASE": "RUN"})
    assert response.status_code == 400
    assert 'value="ERROR"' in response.text
    job.delete()


def test_job_list_and_phase_filter(tap_service):
    svc = _service(tap_service)
    job = svc.submit_job(QUERY)

    listing = ET.fromstring(httpx.get(f"{tap_service}/async").text)
    ids = {ref.get("id") for ref in listing.findall("uws:jobref", UWS)}
    assert job.job_id in ids

    pending = ET.fromstring(httpx.get(f"{tap_service}/async", params={"PHASE": "PENDING"}).text)
    assert job.job_id in {ref.get("id") for ref in pending.findall("uws:jobref", UWS)}

    completed = ET.fromstring(httpx.get(f"{tap_service}/async", params={"PHASE": "COMPLETED"}).text)
    assert job.job_id not in {ref.get("id") for ref in completed.findall("uws:jobref", UWS)}
    job.delete()


def test_results_resource_lists_result(tap_service):
    svc = _service(tap_service)
    job = svc.submit_job("SELECT TOP 1 source_id FROM ska.continuum_sources")
    job.run()
    job.wait(phases=["COMPLETED", "ERROR", "ABORTED"], timeout=60)
    root = ET.fromstring(httpx.get(f"{job.url}/results").text)
    results = root.findall("uws:results/uws:result", UWS)
    assert len(results) == 1
    assert results[0].get("id") == "result"

    result = httpx.get(f"{job.url}/results/result")
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("application/x-votable+xml")
    job.delete()


def test_async_responseformat_csv(tap_service):
    response = httpx.post(
        f"{tap_service}/async",
        data={
            "LANG": "ADQL",
            "QUERY": "SELECT TOP 2 source_name FROM ska.continuum_sources",
            "RESPONSEFORMAT": "csv",
            "PHASE": "RUN",
        },
        follow_redirects=False,
    )
    job_url = response.headers["location"]
    job = pyvo.dal.AsyncTAPJob(job_url)
    job.wait(phases=["COMPLETED", "ERROR", "ABORTED"], timeout=60)
    assert job.phase == "COMPLETED"
    result = httpx.get(f"{job_url}/results/result")
    assert result.headers["content-type"].startswith("text/csv")
    assert result.text.splitlines()[0] == "source_name"
    httpx.delete(job_url)


def test_examples_endpoint(tap_service):
    response = httpx.get(f"{tap_service}/examples")
    assert response.status_code == 200
    assert 'typeof="example"' in response.text
    assert 'property="query"' in response.text
