"""The gates: conformance parsing, agreement fingerprints, VOSI capture."""

from tap_compare import conformance, validate, vosi


def test_taplint_errors_counted_by_stage_and_blocking_set():
    report = (
        "I-CAP-CPDC-1 something informative\n"
        "E-CAP-XVAL-1 capabilities invalid\n"
        "E-EXA-XVAL-1 examples invalid\n"
        "W-QGE-QERR-1 a warning\n"
        "E-QGE-QERR-2 sync query failed\n"
    )
    parsed = conformance.parse_report(report)
    assert parsed["errors_total"] == 3
    # EXA is not a stage the workload exercises, so it reports but not blocks
    assert parsed["errors_blocking"] == 2
    assert parsed["passed"] is False
    assert parsed["by_stage"]["CAP"] == {"I": 1, "E": 1}


def test_a_clean_report_passes():
    assert conformance.parse_report("I-CAP-CPDC-1 fine\nS-QGE-SUMM-1 done\n")["passed"]


def test_fingerprint_is_order_independent():
    """TAP imposes no result order without ORDER BY, so two conformant
    servers may return the same rows differently ordered."""
    a = "obs_publisher_did,s_ra\nivo://x/1,1.0\nivo://x/2,2.0\n"
    b = "obs_publisher_did,s_ra\nivo://x/2,2.0\nivo://x/1,1.0\n"
    assert validate.fingerprint(a) == validate.fingerprint(b)
    assert validate.fingerprint(a)["rows"] == 2


def test_fingerprint_detects_a_missing_row():
    a = "obs_publisher_did\nivo://x/1\nivo://x/2\n"
    b = "obs_publisher_did\nivo://x/1\n"
    assert validate.fingerprint(a) != validate.fingerprint(b)


def test_fingerprint_without_the_key_column_hashes_whole_rows():
    listing = "table_name,description\ntap_schema.tables,tables\n"
    assert validate.fingerprint(listing)["rows"] == 1


CAPABILITIES = b"""<?xml version="1.0"?>
<vosi:capabilities xmlns:vosi="http://www.ivoa.net/xml/VOSICapabilities/v1.0"
    xmlns:tr="http://www.ivoa.net/xml/TAPRegExt/v1.0">
  <capability standardID="ivo://ivoa.net/std/TAP"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="tr:TableAccess">
    <interface xsi:type="vod:ParamHTTP" version="1.1" xmlns:vod="http://www.ivoa.net/xml/VODataService/v1.1">
      <accessURL use="base">http://example/tap</accessURL>
    </interface>
    <tr:outputFormat><tr:mime>text/csv</tr:mime></tr:outputFormat>
    <tr:outputFormat><tr:mime>application/x-votable+xml</tr:mime></tr:outputFormat>
    <tr:outputLimit>
      <tr:default unit="row">10000</tr:default>
      <tr:hard unit="row">1000000</tr:hard>
    </tr:outputLimit>
  </capability>
</vosi:capabilities>
"""


def test_capabilities_facts_are_extracted_namespace_tolerantly():
    facts = vosi._parse_capabilities(CAPABILITIES)
    assert facts["tap_versions"] == ["1.1"]
    assert facts["output_formats"] == ["application/x-votable+xml", "text/csv"]
    assert facts["maxrec_default"] == 10000
    assert facts["maxrec_hard"] == 1000000
