"""Unit tests for cursor typing and TAP_SCHEMA metadata resolution."""

from collections import namedtuple

from tapcore.query.results import ColumnMeta, columns_from_cursor

Desc = namedtuple("Desc", ["name", "type_code"])


def test_columns_from_cursor_maps_oids():
    description = [
        Desc("i8", 20),
        Desc("i4", 23),
        Desc("i2", 21),
        Desc("f8", 701),
        Desc("f4", 700),
        Desc("num", 1700),
        Desc("flag", 16),
        Desc("txt", 25),
        Desc("ts", 1184),
        Desc("mystery", 999999),  # e.g. pg_sphere spoint
    ]
    kinds = [c.kind for c in columns_from_cursor(description, {})]
    assert kinds == [
        "int64",
        "int32",
        "int16",
        "float64",
        "float32",
        "float64",
        "bool",
        "str",
        "timestamp",
        "str",
    ]


def test_columns_from_cursor_attaches_tap_metadata():
    meta = {"ra": {"unit": "deg", "ucd": "pos.eq.ra", "description": "Right ascension"}}
    (col,) = columns_from_cursor([Desc("ra", 701)], meta)
    assert col == ColumnMeta(
        "ra", kind="float64", unit="deg", ucd="pos.eq.ra", description="Right ascension"
    )


def test_tap_schema_metadata_conflicting_descriptions(tap_conn=None):
    """Same unit/UCD but different descriptions across touched tables must
    also be treated as ambiguous (exercised via the pure merge logic)."""
    from unittest.mock import MagicMock

    from tapcore.query.results import tap_schema_metadata

    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        ("ra", "deg", "pos.eq.ra", "Right ascension"),
        ("ra", "deg", "pos.eq.ra", "RA of artifact centre"),
        ("dec", "deg", "pos.eq.dec", "Declination"),
    ]
    meta = tap_schema_metadata(conn, ["t.a", "t.b"])
    assert meta["ra"] == {"unit": None, "ucd": None, "description": None}
    assert meta["dec"]["description"] == "Declination"
