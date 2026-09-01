"""Remote-target provenance: what a TAP service says about itself.

Captured before any timed rung and archived verbatim in the run directory —
a cross-server comparison lives or dies on knowing exactly which software,
versions, limits and capabilities were measured.
"""

from __future__ import annotations

import pathlib
from xml.etree import ElementTree as ET

import httpx

VOSI_CAP = "{http://www.ivoa.net/xml/VOSICapabilities/v1.0}"
TR = "{http://www.ivoa.net/xml/TAPRegExt/v1.0}"


def capture(base_url: str, out_dir: pathlib.Path, timeout_s: float = 30.0) -> dict:
    """Fetch capabilities/tables/availability; archive raw XML; return facts.

    The raw documents are the evidence; the returned dict is the convenient
    summary (TAP version, declared output formats, MAXREC limits).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    facts: dict = {"base_url": base_url}
    with httpx.Client(timeout=timeout_s) as client:
        for resource in ("capabilities", "tables", "availability"):
            response = client.get(f"{base_url}/{resource}")
            response.raise_for_status()
            (out_dir / f"{resource}.xml").write_bytes(response.content)
            facts[f"{resource}_status"] = response.status_code
            if server := response.headers.get("server"):
                facts.setdefault("server_header", server)
    facts.update(_parse_capabilities((out_dir / "capabilities.xml").read_bytes()))
    return facts


def _parse_capabilities(raw: bytes) -> dict:
    """TAP version, output formats and hard limits out of a capabilities doc.

    Namespace-tolerant on purpose: the compared servers disagree about
    exactly which schema versions they stamp on these elements, and the
    *local names* are what TAPRegExt fixes.
    """
    root = ET.fromstring(raw)
    facts: dict = {"tap_versions": [], "output_formats": [], "maxrec_default": None}

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    for capability in root.iter():
        if local(capability.tag) != "capability":
            continue
        standard_id = capability.get("standardID", "")
        if "std/TAP" not in standard_id:
            continue
        for interface in capability.iter():
            if local(interface.tag) == "interface" and (v := interface.get("version")):
                facts["tap_versions"].append(v)
        for element in capability.iter():
            name = local(element.tag)
            if name == "outputFormat":
                for child in element:
                    if local(child.tag) == "mime" and child.text:
                        facts["output_formats"].append(child.text.strip())
            elif name == "outputLimit":
                for child in element:
                    if local(child.tag) == "default" and child.text:
                        facts["maxrec_default"] = int(child.text)
                    if local(child.tag) == "hard" and child.text:
                        facts["maxrec_hard"] = int(child.text)
    facts["tap_versions"] = sorted(set(facts["tap_versions"]))
    facts["output_formats"] = sorted(set(facts["output_formats"]))
    return facts
