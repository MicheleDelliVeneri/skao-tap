"""The ivoa.obscore view definition and its declarations (package 12).

Compliance is a list of exact names, units, UCDs and utypes from
REC-ObsCore-v1.1 Table 6 — so the tests are mostly that list, pinned. The
view's SQL semantics run against real PostgreSQL in the component suite;
here the pinned surface is what a validator (taplint, pyvo) would read.
"""

import pytest
from egernia_api.endpoints import vosi
from egernia_api.plugins import obscore

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
    return {c.name: c for c in obscore.OBSCORE_COLUMNS}


def test_all_thirty_mandatory_columns_in_rec_order():
    names = [c.name for c in obscore.OBSCORE_COLUMNS]
    assert names[: len(MANDATORY)] == MANDATORY
    # the one extra is the package-7 geometry, declared non-standard
    assert names[len(MANDATORY) :] == ["s_region_geom"]
    std = {c.name: c.std for c in obscore.OBSCORE_COLUMNS}
    assert all(std[name] == 1 for name in MANDATORY)
    assert std["s_region_geom"] == 0


def test_char_columns_declare_a_variable_arraysize():
    """VOTable needs arraysize="*" on char and nothing on the fixed-width
    types; it is derived from the datatype, so this is the rule's test."""
    for column in obscore.OBSCORE_COLUMNS:
        assert column.arraysize == ("*" if column.datatype == "char" else None), column.name


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
            "meta.ref.ivoid",
            "obscore:Curation.publisherDID",
        ),
        ("pol_states", None, "meta.code;phys.polarization", None),
    ],
)
def test_units_ucds_and_utypes_match_rec_table_6(name, unit, ucd, utype):
    column = _by_name()[name]
    assert column.unit == unit
    assert column.ucd == ucd
    if utype is not None:
        assert column.utype == utype


def test_s_region_is_declared_a_region():
    assert _by_name()["s_region"].xtype == "adql:REGION"


def test_view_sql_carries_the_mapping_decisions():
    sql = obscore.view_sql()
    assert "WHEN 'table' THEN 'measurements'" in sql
    assert "COALESCE(o.collection, 'unclassified')" in sql
    assert "LEFT JOIN LATERAL" in sql and "art.semantics = 'science'" in sql
    assert "round(a.access_estsize / 1000.0)::bigint" in sql
    assert sql.startswith("CREATE VIEW ivoa.obscore AS")
    assert obscore.view_sql(or_replace=True).startswith("CREATE OR REPLACE VIEW ivoa.obscore AS")


def test_the_did_percent_encodes_every_key_component():
    """A PublisherDID is permanent and the five key columns are free text: a
    product_id holding '/' or a space would forge a path segment or produce
    an identifier no client can parse, and two products could collide on
    one DID."""
    sql = obscore.view_sql()
    did = next(line for line in sql.splitlines() if line.endswith(" AS obs_publisher_did,"))
    assert did.lstrip().startswith("'ivo://skao.int/~?' || ")
    # the separators stay literal; the components do not
    assert did.count(" || '/' || ") == 4
    for column in obscore.did_key_columns():
        assert f"regexp_split_to_table(p.{column}, '')" in did, column
        assert f"CASE WHEN p.{column} ~ '^[{obscore.DID_SAFE_CLASS}]*$'" in did, column
    # unreserved characters survive; anything else becomes %XX per UTF-8 byte
    assert did.count("upper(encode(convert_to(ch, 'UTF8'), 'hex'))") == 5
    assert "p.project_id ||" not in did  # never interpolated raw


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


class _ViewShapeChanged(Exception):
    """What psycopg raises when CREATE OR REPLACE VIEW meets a changed
    column list (SQLSTATE 42P16)."""

    sqlstate = "42P16"


class _RecordingConn:
    """Just enough connection for ensure_obscore: records statements, answers
    the relkind/comment and quoting probes, and optionally refuses the view
    replacement the way Postgres does when the column list changed.

    Params are recorded alongside each statement, not discarded: Postgres
    plans no parameters for DDL, so a statement's *bindability* is part of
    what these tests have to be able to see."""

    def __init__(self, relkind: str | None, comment: str | None = None, refuse_replace=False):
        self._relkind = relkind
        self._comment = comment
        self._refuse_replace = refuse_replace
        self.statements: list[str] = []
        self.calls: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.calls.append((sql, params))
        if self._refuse_replace and sql.startswith("CREATE OR REPLACE VIEW"):
            raise _ViewShapeChanged("cannot change name of view column")
        relkind, comment = self._relkind, self._comment

        class Result:
            def fetchone(self):
                if "pg_class" in sql:
                    return (relkind, comment) if relkind else None
                if "quote_literal" in sql:
                    return ("'" + str(params[0]).replace("'", "''") + "'",)
                return None

        return Result()


def test_calib_level_description_does_not_claim_obscores_vocabulary():
    """srcnet reads level 1 as calibrated where ObsCore 1.1 reads it as
    instrumental, and the view passes the value through untranslated: the
    description has to describe the column, not the standard."""
    description = _by_name()["calib_level"].description
    assert "1=calibrated" in description
    assert "instrumental" not in description
    assert "untranslated" in description


def test_a_preexisting_obscore_table_is_left_alone():
    """The benchmark suite's synthetic ivoa.obscore is a real table, and so
    is any archive's own: the bootstrap must neither destroy it nor crash on
    it — it is that deployment's ObsCore."""
    conn = _RecordingConn("r")
    obscore.ensure_obscore(conn)
    assert not any("DROP VIEW" in s or "CREATE VIEW" in s for s in conn.statements)
    assert not any("tap_schema" in s for s in conn.statements)


def test_a_view_whose_definition_did_not_change_is_not_touched():
    """DROP + CREATE and CREATE OR REPLACE both take ACCESS EXCLUSIVE on the
    view for the rest of the bootstrap transaction, and the bootstrap runs on
    every pod start — so the unchanged case must issue no view DDL at all,
    only the lock-free registration."""
    conn = _RecordingConn("v", comment=obscore.definition_comment(obscore.view_sql()))
    obscore.ensure_obscore(conn)
    assert not any("VIEW" in s for s in conn.statements)
    assert any("tap_schema.columns" in s for s in conn.statements)


def test_a_stale_view_is_replaced_in_place():
    conn = _RecordingConn("v", comment="ObsCore 1.1 over the ODP metadata (definition older0)")
    obscore.ensure_obscore(conn)
    assert any(s.startswith("CREATE OR REPLACE VIEW ivoa.obscore") for s in conn.statements)
    assert not any("DROP VIEW" in s for s in conn.statements)
    assert conn.statements.count("SAVEPOINT obscore_view") == 1
    assert "RELEASE SAVEPOINT obscore_view" in conn.statements
    assert any(s.startswith("COMMENT ON VIEW ivoa.obscore") for s in conn.statements)


def test_the_view_comment_is_quoted_into_the_ddl_not_bound_as_a_parameter():
    """COMMENT ON is DDL and Postgres plans no parameters for it: binding the
    fingerprint raises 42601 and takes the whole metadata bootstrap — hence
    every pod's startup — down with it. The comment must reach the statement
    already quoted."""
    conn = _RecordingConn("v", comment="stale")
    obscore.ensure_obscore(conn)
    ddl_keywords = ("COMMENT", "CREATE", "DROP", "GRANT", "ALTER")
    ddl = [(s, p) for s, p in conn.calls if s.startswith(ddl_keywords)]
    assert all(p is None for _, p in ddl), [s for s, p in ddl if p is not None]
    comment_ddl = next(s for s, _ in ddl if s.startswith("COMMENT ON VIEW ivoa.obscore"))
    assert obscore.definition_comment(obscore.view_sql()) in comment_ddl
    assert "%s" not in comment_ddl


def test_a_changed_column_list_falls_back_to_drop_and_create():
    """CREATE OR REPLACE VIEW is refused (42P16) when the column list moves,
    and the failed statement has aborted the transaction — so the fallback
    has to roll back to the savepoint before it can run."""
    conn = _RecordingConn("v", comment="stale", refuse_replace=True)
    obscore.ensure_obscore(conn)
    order = [s.split("\n")[0] for s in conn.statements]
    assert order.index("ROLLBACK TO SAVEPOINT obscore_view") < order.index(
        "DROP VIEW IF EXISTS ivoa.obscore"
    )
    assert any(s.startswith("CREATE VIEW ivoa.obscore") for s in conn.statements)


def test_a_first_creation_needs_no_existing_view():
    conn = _RecordingConn(None)
    obscore.ensure_obscore(conn)
    assert any(s.startswith("CREATE OR REPLACE VIEW ivoa.obscore") for s in conn.statements)


def test_an_unrelated_database_error_is_not_swallowed_as_a_shape_change():
    class _Boom(_RecordingConn):
        def execute(self, sql, params=None):
            if sql.startswith("CREATE OR REPLACE VIEW"):
                raise RuntimeError("connection lost")
            return super().execute(sql, params)

    with pytest.raises(RuntimeError, match="connection lost"):
        obscore.ensure_obscore(_Boom("v", comment="stale"))


def test_the_did_chain_is_configurable_from_the_model_hierarchy(auth_settings):
    """A deployment whose ODP model nests differently has a different
    identity chain, so the DID path is configured rather than compiled in."""
    auth_settings(obscore_did_columns="project_id.obs_id.product_id")
    assert obscore.did_key_columns() == ("project_id", "obs_id", "product_id")
    did = next(
        line for line in obscore.view_sql().splitlines() if line.endswith(" AS obs_publisher_did,")
    )
    assert did.count(" || '/' || ") == 2  # three components, two separators
    assert "p.sbd_id" not in did and "p.eb_id" not in did
    for column in ("project_id", "obs_id", "product_id"):
        # each component still percent-encoded, not interpolated raw
        assert f"regexp_split_to_table(p.{column}, '')" in did


def test_the_default_chain_is_the_data_products_primary_key():
    """The default is not an arbitrary list: it is what makes a DID unique."""
    from egernia_api.plugins.odp import PLUGIN

    data_products = next(t for t in PLUGIN.tables if t.name == "data_products")
    assert obscore.did_key_columns() == tuple(c.name for c in data_products.columns if c.is_key)


@pytest.mark.parametrize(
    "configured",
    [
        "project_id; DROP VIEW ivoa.obscore --",
        "project_id, obs_id",
        "Project_ID",
        "'project_id'",
        "project_id.obs id",
        "",
        "   ",
    ],
)
def test_a_did_column_outside_the_identifier_alphabet_is_refused(auth_settings, configured):
    """These names are interpolated into a CREATE VIEW, where nothing can be
    bound, so the alphabet is closed rather than escaped."""
    auth_settings(obscore_did_columns=configured)
    with pytest.raises(ValueError, match="TAP_OBSCORE_DID_COLUMNS"):
        obscore.view_sql()


def test_a_qualified_name_is_read_as_two_components(auth_settings):
    """The separator is a dot, so `p.project_id` — the spelling the view's own
    SQL uses — is a two-column chain, not one qualified column. It is refused
    by PostgreSQL at bootstrap rather than here, because `p` is a syntactically
    valid column name that simply does not exist."""
    auth_settings(obscore_did_columns="p.project_id")
    assert obscore.did_key_columns() == ("p", "project_id")
