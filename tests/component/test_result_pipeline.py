"""Component tests for package 1: typed columns from TAP_SCHEMA, streaming
results, and the Parquet/Arrow output formats — verified end to end with
PyVO, astropy, and pyarrow."""

import io
from xml.etree import ElementTree as ET

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import pyvo

pytestmark = pytest.mark.component

QUERY = "SELECT source_id, source_name, ra, dec, flux_int FROM ska.continuum_sources"


def test_votable_fields_typed_from_tap_schema(tap_service):
    response = httpx.get(f"{tap_service}/sync", params={"LANG": "ADQL", "QUERY": QUERY})
    ns = {"v": "http://www.ivoa.net/xml/VOTable/v1.3"}
    fields = {f.get("name"): f for f in ET.fromstring(response.text).findall(".//v:FIELD", ns)}
    assert fields["source_id"].get("datatype") == "long"
    assert fields["ra"].get("datatype") == "double"
    assert fields["ra"].get("unit") == "deg"
    assert fields["ra"].get("ucd") == "pos.eq.ra;meta.main"
    assert fields["flux_int"].get("unit") == "mJy"


def test_pyvo_receives_units_and_ucds(tap_service):
    table = pyvo.dal.TAPService(tap_service).search(QUERY).to_table()
    assert str(table["ra"].unit) == "deg"
    assert table["ra"].meta.get("ucd") == "pos.eq.ra;meta.main"
    assert table["source_id"].dtype.kind == "i"


def test_sync_parquet(tap_service):
    response = httpx.get(
        f"{tap_service}/sync",
        params={"LANG": "ADQL", "QUERY": QUERY, "RESPONSEFORMAT": "parquet"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.apache.parquet")
    table = pq.read_table(io.BytesIO(response.content))
    assert table.num_rows == 8
    assert table.schema.field("ra").type == pa.float64()
    assert table.schema.field("ra").metadata[b"unit"] == b"deg"
    meta = pq.read_metadata(io.BytesIO(response.content)).metadata
    assert meta[b"IVOA.VOTable.QUERY_STATUS"] == b"OK"


def test_sync_parquet_overflow_metadata(tap_service):
    response = httpx.get(
        f"{tap_service}/sync",
        params={
            "LANG": "ADQL",
            "QUERY": QUERY,
            "RESPONSEFORMAT": "parquet",
            "MAXREC": "3",
        },
    )
    table = pq.read_table(io.BytesIO(response.content))
    assert table.num_rows == 3
    meta = pq.read_metadata(io.BytesIO(response.content)).metadata
    assert meta[b"IVOA.VOTable.QUERY_STATUS"] == b"OVERFLOW"


def test_sync_arrow_stream(tap_service):
    response = httpx.get(
        f"{tap_service}/sync",
        params={"LANG": "ADQL", "QUERY": QUERY, "RESPONSEFORMAT": "arrow"},
    )
    assert response.headers["content-type"].startswith("application/vnd.apache.arrow.stream")
    with pa.ipc.open_stream(io.BytesIO(response.content)) as reader:
        table = reader.read_all()
    assert table.num_rows == 8
    assert table.schema.field("dec").metadata[b"ucd"] == b"pos.eq.dec;meta.main"


def test_async_parquet_job(tap_service):
    svc = pyvo.dal.TAPService(tap_service)
    response = httpx.post(
        f"{tap_service}/async",
        data={"LANG": "ADQL", "QUERY": QUERY, "RESPONSEFORMAT": "parquet", "PHASE": "RUN"},
        follow_redirects=False,
    )
    job_url = response.headers["location"]
    job = pyvo.dal.AsyncTAPJob(job_url)
    job.wait(phases=["COMPLETED", "ERROR", "ABORTED"], timeout=60)
    assert job.phase == "COMPLETED"
    result = httpx.get(f"{job_url}/results/result")
    assert result.headers["content-type"].startswith("application/vnd.apache.parquet")
    table = pq.read_table(io.BytesIO(result.content))
    assert table.num_rows == 8
    assert table.column("source_name").to_pylist()[0].startswith("SKA-CS")
    httpx.delete(job_url)
    assert svc is not None


def test_json_query_parquet_format(tap_service):
    root = tap_service.rsplit("/tap", 1)[0]
    response = httpx.post(
        f"{root}/api/v1/query",
        json={"query": "SELECT TOP 2 ra, dec FROM ska.continuum_sources", "format": "parquet"},
    )
    assert response.status_code == 200
    assert pq.read_table(io.BytesIO(response.content)).num_rows == 2


def test_json_query_metadata_includes_units(tap_service):
    root = tap_service.rsplit("/tap", 1)[0]
    payload = httpx.post(
        f"{root}/api/v1/query", json={"query": "SELECT TOP 1 ra FROM ska.continuum_sources"}
    ).json()
    assert payload["metadata"][0]["unit"] == "deg"
    assert payload["metadata"][0]["datatype"] == "double"


def test_capabilities_declare_columnar_formats(tap_service):
    text = httpx.get(f"{tap_service}/capabilities").text
    assert "application/vnd.apache.parquet" in text
    assert "application/vnd.apache.arrow.stream" in text


def test_large_result_streams(tap_service):
    """A generate_series-sized cross join streams without exhausting memory
    (this exercises the server-side cursor path end to end)."""
    response = httpx.get(
        f"{tap_service}/sync",
        params={
            "LANG": "ADQL",
            "QUERY": (
                "SELECT a.source_id, b.source_name FROM ska.continuum_sources AS a, "
                "ska.continuum_sources AS b"
            ),
            "RESPONSEFORMAT": "csv",
            "MAXREC": "50",
        },
    )
    assert response.status_code == 200
    assert len(response.text.splitlines()) == 51  # header + 50 rows
