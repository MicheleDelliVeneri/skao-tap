"""Unit tests for the typed streaming result pipeline."""

import datetime
import io
import json
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest
from tapcore.query.results import ColumnMeta, RowLimiter, stream
from tapcore.query.votable import error_votable, normalize_format, serialize

VOT_NS = {"v": "http://www.ivoa.net/xml/VOTable/v1.3"}

COLUMNS = [
    ColumnMeta("id", kind="int64", ucd="meta.id"),
    ColumnMeta("name", kind="str"),
    ColumnMeta("flux", kind="float64", unit="mJy", description="integrated flux"),
    ColumnMeta("obs", kind="timestamp"),
    ColumnMeta("ok", kind="bool"),
]
ROWS = [
    (1, "alpha", Decimal("1.5"), datetime.datetime(2026, 1, 1), True),
    (2, None, None, None, None),
]


def _materialize(columns, rows, fmt, maxrec=None):
    limiter = RowLimiter(rows, len(rows) if maxrec is None else maxrec)
    return b"".join(stream(columns, limiter, fmt)), limiter


def test_normalize_format_aliases_and_mimes():
    assert normalize_format(None) == ("votable", "application/x-votable+xml", "vot")
    assert normalize_format("text/csv") == ("csv", "text/csv", "csv")
    assert normalize_format("TSV")[0] == "tsv"
    assert normalize_format("application/json")[0] == "json"
    assert normalize_format("parquet") == ("parquet", "application/vnd.apache.parquet", "parquet")
    assert normalize_format("arrow")[1] == "application/vnd.apache.arrow.stream"
    with pytest.raises(ValueError):
        normalize_format("fits")


def test_votable_fields_carry_types_units_ucds():
    body, _ = _materialize(COLUMNS, ROWS, "votable")
    root = ET.fromstring(body.decode())
    fields = {f.get("name"): f for f in root.findall(".//v:FIELD", VOT_NS)}
    assert fields["id"].get("datatype") == "long"
    assert fields["id"].get("ucd") == "meta.id"
    assert fields["flux"].get("datatype") == "double"
    assert fields["flux"].get("unit") == "mJy"
    assert fields["flux"].find("v:DESCRIPTION", VOT_NS).text == "integrated flux"
    assert fields["obs"].get("xtype") == "timestamp"
    assert fields["ok"].get("datatype") == "boolean"
    assert fields["name"].get("arraysize") == "*"


def test_votable_rows_and_nulls():
    body, _ = _materialize(COLUMNS, ROWS, "votable")
    root = ET.fromstring(body.decode())
    trs = root.findall(".//v:TR", VOT_NS)
    assert len(trs) == 2
    first = [td.text for td in trs[0].findall("v:TD", VOT_NS)]
    assert first == ["1", "alpha", "1.5", "2026-01-01T00:00:00", "true"]
    second = [td.text for td in trs[1].findall("v:TD", VOT_NS)]
    assert second == ["2", None, None, None, None]


def test_votable_parses_with_astropy_masks_and_units():
    from astropy.io.votable import parse_single_table

    body, _ = _materialize(COLUMNS, ROWS, "votable")
    table = parse_single_table(io.BytesIO(body)).to_table()
    assert str(table["flux"].unit) == "mJy"
    assert bool(table["flux"].mask[1])
    assert int(table["id"][1]) == 2


def test_votable_overflow_is_trailing_info():
    body, limiter = _materialize(COLUMNS, ROWS, "votable", maxrec=1)
    assert limiter.overflowed
    root = ET.fromstring(body.decode())
    infos = root.findall(".//v:INFO", VOT_NS)
    assert infos[-1].get("value") == "OVERFLOW"
    # the trailing INFO must come after the TABLE element (DALI streaming rule)
    resource = root.find("v:RESOURCE", VOT_NS)
    children = [c.tag.split("}")[1] for c in resource]
    assert children.index("TABLE") < len(children) - 1
    assert len(root.findall(".//v:TR", VOT_NS)) == 1


def test_maxrec_zero_yields_metadata_only_with_overflow():
    body, limiter = _materialize(COLUMNS, ROWS, "votable", maxrec=0)
    root = ET.fromstring(body.decode())
    assert len(root.findall(".//v:FIELD", VOT_NS)) == len(COLUMNS)
    assert root.findall(".//v:TR", VOT_NS) == []
    assert limiter.status == "OVERFLOW"


def test_csv_and_tsv():
    body, _ = _materialize(COLUMNS, ROWS, "csv")
    lines = body.decode().splitlines()
    assert lines[0] == "id,name,flux,obs,ok"
    assert lines[1] == "1,alpha,1.5,2026-01-01T00:00:00,True"
    assert lines[2] == "2,,,,"
    tsv, _ = _materialize(COLUMNS, ROWS, "tsv")
    assert tsv.decode().splitlines()[0] == "id\tname\tflux\tobs\tok"


def test_json_carries_metadata_and_status():
    body, _ = _materialize(COLUMNS, ROWS, "json", maxrec=1)
    payload = json.loads(body)
    assert payload["status"] == "OVERFLOW"
    assert payload["metadata"][2] == {
        "name": "flux",
        "datatype": "double",
        "unit": "mJy",
        "ucd": None,
        "description": "integrated flux",
    }
    assert payload["data"] == [[1, "alpha", 1.5, "2026-01-01T00:00:00", True]]


def test_parquet_roundtrip_with_metadata():
    import pyarrow.parquet as pq

    body, _ = _materialize(COLUMNS, ROWS, "parquet")
    table = pq.read_table(io.BytesIO(body))
    assert table.num_rows == 2
    assert table.column("id").to_pylist() == [1, 2]
    assert table.column("flux").to_pylist() == [1.5, None]
    flux_meta = table.schema.field("flux").metadata
    assert flux_meta[b"unit"] == b"mJy"
    file_meta = pq.read_metadata(io.BytesIO(body)).metadata
    assert file_meta[b"IVOA.VOTable.QUERY_STATUS"] == b"OK"


def test_parquet_overflow_status_and_empty_result():
    import pyarrow.parquet as pq

    body, _ = _materialize(COLUMNS, ROWS, "parquet", maxrec=1)
    table = pq.read_table(io.BytesIO(body))
    assert table.num_rows == 1
    file_meta = pq.read_metadata(io.BytesIO(body)).metadata
    assert file_meta[b"IVOA.VOTable.QUERY_STATUS"] == b"OVERFLOW"

    empty, _ = _materialize(COLUMNS, [], "parquet")
    table = pq.read_table(io.BytesIO(empty))
    assert table.num_rows == 0
    assert table.schema.names == [c.name for c in COLUMNS]


def test_arrow_ipc_stream_roundtrip():
    import pyarrow as pa

    body, _ = _materialize(COLUMNS, ROWS, "arrow")
    with pa.ipc.open_stream(io.BytesIO(body)) as reader:
        table = reader.read_all()
    assert table.num_rows == 2
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("flux").metadata[b"unit"] == b"mJy"


def test_serialize_convenience_wrapper():
    body = serialize(["a", "b"], [(1, "x")], "csv")
    assert body.decode().splitlines() == ["a,b", "1,x"]


def test_error_votable_is_dali_error_document():
    root = ET.fromstring(error_votable("something <bad> happened").decode())
    info = root.find(".//v:INFO", VOT_NS)
    assert info.get("name") == "QUERY_STATUS"
    assert info.get("value") == "ERROR"
    assert "something <bad> happened" in info.text
