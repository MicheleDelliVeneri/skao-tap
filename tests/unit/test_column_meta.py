"""Unit tests for cursor typing and TAP_SCHEMA metadata resolution."""

from collections import namedtuple

from tapcore.results import ColumnMeta, columns_from_cursor

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
