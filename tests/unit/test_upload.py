"""Unit tests for TAP table upload: parsing, limits, temp-table plumbing."""

import pytest
from tapcore.errors import UsageError
from tapcore.query.upload import (
    UploadedTable,
    create_upload_tables,
    parse_upload_param,
    parse_votable,
    rewrite_upload_refs,
    table_ident,
)

VOTABLE = b"""<?xml version="1.0"?>
<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">
<RESOURCE><TABLE>
<FIELD name="id" datatype="long"/>
<FIELD name="ra" datatype="double"/>
<FIELD name="ok" datatype="boolean"/>
<FIELD name="seen" datatype="char" arraysize="19" xtype="timestamp"/>
<FIELD name="label" datatype="char" arraysize="*"/>
<DATA><TABLEDATA>
<TR><TD>1</TD><TD>62.1</TD><TD>true</TD><TD>2026-01-02T03:04:05</TD><TD>a</TD></TR>
<TR><TD>2</TD><TD>NaN</TD><TD>F</TD><TD>2026-01-02T03:04:06</TD><TD/></TR>
</TABLEDATA></DATA>
</TABLE></RESOURCE></VOTABLE>"""


def test_parse_upload_param_pairs():
    assert parse_upload_param("t1,param:t1") == [("t1", "param:t1")]
    assert parse_upload_param("a,param:x;b,https://ex.org/t.vot") == [
        ("a", "param:x"),
        ("b", "https://ex.org/t.vot"),
    ]


@pytest.mark.parametrize(
    "value",
    ["t1", "t1,", ",param:x", "1bad,param:x", "t;me,param:x", "t1,param:x;t1,param:y", ";"],
)
def test_parse_upload_param_rejects(value):
    with pytest.raises(UsageError):
        parse_upload_param(value)


def test_parse_votable_types_and_nulls():
    table = parse_votable("t1", VOTABLE, max_rows=10, max_bytes=10_000)
    assert table.columns == [
        ("id", "bigint"),
        ("ra", "double precision"),
        ("ok", "boolean"),
        ("seen", "timestamp"),
        ("label", "text"),
    ]
    assert table.rows[0] == (1, 62.1, True, "2026-01-02T03:04:05", "a")
    assert table.rows[1] == (2, None, False, "2026-01-02T03:04:06", None)
    assert table.ident == "pg_temp.tap_upload_t1"


def test_parse_votable_limits():
    with pytest.raises(UsageError, match="row limit"):
        parse_votable("t1", VOTABLE, max_rows=1, max_bytes=10_000)
    with pytest.raises(UsageError, match="byte limit"):
        parse_votable("t1", VOTABLE, max_rows=10, max_bytes=10)


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (b"not xml at all <", "not well-formed"),
        (b"<root/>", "not a VOTable"),
        (b'<VOTABLE xmlns="http://www.ivoa.net/xml/VOTable/v1.3"/>', "no TABLE"),
        (
            b'<VOTABLE><RESOURCE><TABLE><FIELD name="a" datatype="int"/>'
            b"<DATA><BINARY/></DATA></TABLE></RESOURCE></VOTABLE>",
            "TABLEDATA",
        ),
        (
            b"<VOTABLE><RESOURCE><TABLE><DATA><TABLEDATA/></DATA></TABLE></RESOURCE></VOTABLE>",
            "no FIELDs",
        ),
        (
            b'<VOTABLE><RESOURCE><TABLE><FIELD name="a" datatype="int"/>'
            b'<FIELD name="a" datatype="int"/></TABLE></RESOURCE></VOTABLE>',
            "duplicate column",
        ),
        (
            b'<VOTABLE><RESOURCE><TABLE><FIELD name="a;b" datatype="int"/>'
            b"</TABLE></RESOURCE></VOTABLE>",
            "invalid column name",
        ),
        (
            b'<VOTABLE><RESOURCE><TABLE><FIELD name="a" datatype="int"/>'
            b"<DATA><TABLEDATA><TR><TD>1</TD><TD>2</TD></TR></TABLEDATA></DATA>"
            b"</TABLE></RESOURCE></VOTABLE>",
            "expected 1",
        ),
    ],
)
def test_parse_votable_rejects(data, match):
    with pytest.raises(UsageError, match=match):
        parse_votable("t1", data, max_rows=10, max_bytes=10_000)


def test_non_char_array_column_becomes_text():
    data = (
        b'<VOTABLE><RESOURCE><TABLE><FIELD name="vec" datatype="double" arraysize="3"/>'
        b"<DATA><TABLEDATA><TR><TD>1 2 3</TD></TR></TABLEDATA></DATA>"
        b"</TABLE></RESOURCE></VOTABLE>"
    )
    table = parse_votable("t1", data, max_rows=10, max_bytes=10_000)
    assert table.columns == [("vec", "text")]
    assert table.rows == [("1 2 3",)]


def test_rewrite_upload_refs_case_insensitive():
    sql = "SELECT * FROM TAP_UPLOAD.T1 JOIN tap_upload.t2 ON TAP_UPLOAD.T1.a = tap_upload.t2.a"
    rewritten = rewrite_upload_refs(sql, {"t1", "t2"})
    assert "TAP_UPLOAD" not in rewritten.upper().replace("PG_TEMP.TAP_UPLOAD_", "")
    assert table_ident("t1") in rewritten
    assert table_ident("t2") in rewritten


def test_create_upload_tables_batches_inserts(fake_db):
    from tapcore.db import pool

    upload = UploadedTable(
        name="t1",
        columns=[("id", "bigint")],
        rows=[(i,) for i in range(1201)],
    )
    with pool().connection() as conn:
        create_upload_tables(conn, [upload], "tap_reader")
    creates = [s for s in fake_db.statements if s.startswith("CREATE TEMP TABLE")]
    grants = [s for s in fake_db.statements if s.startswith("GRANT SELECT")]
    inserts = [s for s in fake_db.statements if s.startswith("INSERT INTO pg_temp.tap_upload_t1")]
    assert creates == ["CREATE TEMP TABLE tap_upload_t1 (id bigint) ON COMMIT DROP"]
    assert grants == ["GRANT SELECT ON pg_temp.tap_upload_t1 TO tap_reader"]
    assert len(inserts) == 3  # 500 + 500 + 201
