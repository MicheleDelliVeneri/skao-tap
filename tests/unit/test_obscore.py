"""The ivoa.obscore view definition and its declarations (package 12).

Compliance is a list of exact names, units, UCDs and utypes from
REC-ObsCore-v1.1 Table 6 — so the tests are mostly that list, pinned. The
view's SQL semantics run against real PostgreSQL in the component suite;
here the pinned surface is what a validator (taplint, pyvo) would read.
"""

import pytest
from tap_api.endpoints import vosi
from tap_api.plugins import obscore

MANDATORY = [
    "dataproduct_type",
    "calib_level",
    "obs_collection",
    "obs_id",
    "obs_publisher_did",
    "access_url",
    "access_format",
    "access_estsize",
    "target_name",
    "s_ra",
    "s_dec",
    "s_fov",
    "s_region",
    "s_resolution",
    "s_xel1",
    "s_xel2",
    "t_min",
    "t_max",
    "t_exptime",
    "t_resolution",
    "t_xel",
    "em_min",
    "em_max",
    "em_res_power",
    "em_xel",
    "o_ucd",
    "pol_states",
    "pol_xel",
    "facility_name",
    "instrument_name",
]


def _by_name():
    return {c[0]: c for c in obscore.OBSCORE_COLUMNS}


def test_all_thirty_mandatory_columns_in_rec_order():
    names = [c[0] for c in obscore.OBSCORE_COLUMNS]
    assert names[: len(MANDATORY)] == MANDATORY
    # the one extra is the package-7 geometry, declared non-standard
    assert names[len(MANDATORY) :] == ["s_region_geom"]
    std = {name: c[8] for name, c in zip(names, obscore.OBSCORE_COLUMNS, strict=True)}
    assert all(std[name] == 1 for name in MANDATORY)
    assert std["s_region_geom"] == 0


@pytest.mark.parametrize(
    ("name", "unit", "ucd", "utype"),
    [
        ("access_estsize", "kbyte", "phys.size;meta.file", "obscore:Access.size"),
        ("s_ra", "deg", "pos.eq.ra", None),
        ("s_resolution", "arcsec", "pos.angResolution", None),
        ("t_min", "d", "time.start;obs.exposure", None),
        ("t_exptime", "s", "time.duration;obs.exposure", None),
        ("em_min", "m", "em.wl;stat.min", None),
        (
            "obs_publisher_did",
            None,
            "meta.ref.uri;meta.curation",
            "obscore:Curation.publisherDID",
        ),
        ("pol_states", None, "meta.code;phys.polarization", None),
    ],
)
def test_units_ucds_and_utypes_match_rec_table_6(name, unit, ucd, utype):
    column = _by_name()[name]
    assert column[4] == unit
    assert column[5] == ucd
    if utype is not None:
        assert column[6] == utype


def test_s_region_is_declared_a_region():
    assert _by_name()["s_region"][3] == "adql:REGION"


def test_view_sql_carries_the_mapping_decisions():
    sql = obscore.view_sql()
    assert "WHEN 'table' THEN 'measurements'" in sql
    assert "COALESCE(o.collection, 'unclassified')" in sql
    assert "LEFT JOIN LATERAL" in sql and "art.semantics = 'science'" in sql
    assert "round(a.access_estsize / 1000.0)::bigint" in sql
    assert (
        "'ivo://skao.int/~?' || p.project_id || '/' || p.obs_id || '/' || p.sbd_id"
        " || '/' || p.eb_id || '/' || p.product_id AS obs_publisher_did" in sql
    )


def test_did_prefix_outside_the_identifier_alphabet_is_refused(auth_settings):
    auth_settings(obscore_did_prefix="ivo://x/'; DROP VIEW --")
    with pytest.raises(ValueError, match="identifier alphabet"):
        obscore.view_sql()


def test_capabilities_declare_the_data_model_with_odp_active():
    xml = vosi.capabilities_xml()
    assert '<dataModel ivo-id="ivo://ivoa.net/std/ObsCore#core-1.1">ObsCore-1.1</dataModel>' in xml
    # TAPRegExt order: after the interface, before the language
    assert xml.index("</interface>") < xml.index("<dataModel") < xml.index("<language>")


def test_capabilities_stay_silent_without_the_odp_plugin(monkeypatch):
    monkeypatch.setattr(vosi, "active_plugins", lambda: [])
    assert "dataModel" not in vosi.capabilities_xml()


def test_registry_record_inherits_the_data_model(auth_settings):
    auth_settings(
        registry_enabled=True,
        registry_identifier="ivo://skao.int/srcnet/tap",
        registry_title="t",
        registry_short_name="t",
        registry_description="d",
        registry_reference_url="https://example.org",
        registry_publisher="p",
        registry_creator="c",
        registry_contact_name="n",
        registry_contact_email="e@example.org",
        registry_subjects="radio",
        registry_created="2026-08-23",
    )
    assert "ObsCore-1.1</dataModel>" in vosi.voresource_xml()
