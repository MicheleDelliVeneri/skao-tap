"""UWS 1.1 XML rendering for the job model persisted by :mod:`egernia_core.uws`.

Only the API renders UWS documents; keeping the ElementTree code here means
the executor and bootstrap import the persistence module without touching it.
"""

from xml.etree import ElementTree as ET

from .config import base_url
from .uws import iso_utc

UWS_NS = "http://www.ivoa.net/xml/UWS/v1.0"
XLINK_NS = "http://www.w3.org/1999/xlink"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


def _el(parent, tag, text=None, nil=False, **attrs):
    element = ET.SubElement(parent, f"{{{UWS_NS}}}{tag}", **attrs)
    if nil:
        element.set(f"{{{XSI_NS}}}nil", "true")
    elif text is not None:
        element.text = str(text)
    return element


def result_url(job_id: str) -> str:
    return f"{base_url()}/async/{job_id}/results/result"


def job_xml(job: dict) -> bytes:
    def _nillable(tag: str, value) -> None:
        if value is None:
            _el(root, tag, nil=True)
        else:
            _el(root, tag, value)

    root = ET.Element(f"{{{UWS_NS}}}job", {"version": "1.1"})
    _el(root, "jobId", job["job_id"])
    _nillable("runId", job["run_id"])
    _nillable("ownerId", job["owner_id"])
    _el(root, "phase", job["phase"])
    _nillable("quote", iso_utc(job["quote"]))
    _el(root, "creationTime", iso_utc(job["creation_time"]))
    _nillable("startTime", iso_utc(job["start_time"]))
    _nillable("endTime", iso_utc(job["end_time"]))
    _el(root, "executionDuration", job["execution_duration"])
    _el(root, "destruction", iso_utc(job["destruction"]))

    params = _el(root, "parameters")
    for key, value in (job["parameters"] or {}).items():
        param = _el(params, "parameter", value)
        param.set("id", key.lower())

    results = _el(root, "results")
    if job["phase"] == "COMPLETED":
        result = _el(results, "result")
        result.set("id", "result")
        result.set(f"{{{XLINK_NS}}}href", result_url(job["job_id"]))
        if job["result_mime"]:
            result.set("mime-type", job["result_mime"])

    if job["phase"] == "ERROR" and job["error_message"]:
        err = _el(root, "errorSummary")
        err.set("type", job["error_type"] or "fatal")
        err.set("hasDetail", "true")
        _el(err, "message", job["error_message"])

    return _serialize(root)


def joblist_xml(jobs: list[dict]) -> bytes:
    root = ET.Element(f"{{{UWS_NS}}}jobs", {"version": "1.1"})
    for job in jobs:
        ref = _el(root, "jobref")
        ref.set("id", job["job_id"])
        ref.set(f"{{{XLINK_NS}}}href", f"{base_url()}/async/{job['job_id']}")
        _el(ref, "phase", job["phase"])
    return _serialize(root)


def _serialize(root) -> bytes:
    ET.register_namespace("uws", UWS_NS)
    ET.register_namespace("xlink", XLINK_NS)
    ET.register_namespace("xsi", XSI_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
