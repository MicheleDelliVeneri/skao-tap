"""Unit tests for the pydantic->PostgreSQL schema generator, run against the
real ska-src-mm-notification models."""

from ska_src_mm_notification.models.schemas.srcnet_ingestion import SRCIngestionNotification
from tap_api.schema_gen import build_tables, ddl_statements, registration_statements

TABLES = build_tables(SRCIngestionNotification, "srcnet", "projects")
BY_NAME = {t.name: t for t in TABLES}


def test_hierarchy_becomes_tables_in_creation_order():
    assert [t.name for t in TABLES] == [
        "projects",
        "observations",
        "scheduling_blocks",
        "execution_blocks",
        "data_products",
        "artifacts",
    ]


def test_primary_keys_follow_identity_chain():
    assert BY_NAME["projects"].pk_columns == ["project_id"]
    assert BY_NAME["artifacts"].pk_columns == [
        "project_id",
        "obs_id",
        "sbd_id",
        "eb_id",
        "product_id",
        "artifact_id",
    ]


def test_scalar_type_mapping():
    cols = {c.name: c for c in BY_NAME["artifacts"].columns}
    assert cols["access_url"].sql_type == "text"
    assert cols["access_estsize"].sql_type == "bigint"
    assert cols["s_ra"].sql_type == "double precision"
    assert not cols["access_url"].nullable
    assert cols["s_ra"].nullable
    project_cols = {c.name: c for c in BY_NAME["projects"].columns}
    assert project_cols["group_ids"].sql_type == "jsonb"


def test_field_constraints_become_checks():
    cols = {c.name: c for c in BY_NAME["artifacts"].columns}
    assert "s_dec >= -90.0" in cols["s_dec"].checks
    assert "s_dec <= 90.0" in cols["s_dec"].checks
    assert "s_fov > 0.0" in cols["s_fov"].checks
    assert any("semantics IN ('science'" in chk for chk in cols["semantics"].checks)


def test_enum_check_on_dataproduct_type():
    cols = {c.name: c for c in BY_NAME["data_products"].columns}
    (check,) = cols["dataproduct_type"].checks
    for value in ("image", "cube", "visibility"):
        assert f"'{value}'" in check


def test_ddl_is_idempotent_and_grants_reader():
    statements = ddl_statements(TABLES, "tap_reader")
    assert statements[0] == "CREATE SCHEMA IF NOT EXISTS srcnet"
    assert all("IF NOT EXISTS" in s for s in statements[1:-2])  # CREATEs and ALTERs
    assert any(
        s.startswith("CREATE TABLE IF NOT EXISTS srcnet.observations")
        and "FOREIGN KEY (project_id) REFERENCES srcnet.projects" in s
        for s in statements
    )
    assert any("GRANT SELECT ON ALL TABLES IN SCHEMA srcnet TO tap_reader" in s for s in statements)


def test_tap_schema_registration_covers_all_tables_and_columns():
    stmts = registration_statements(TABLES, "test")
    inserts = [s for s, _ in stmts]
    assert sum("tap_schema.tables" in s for s in inserts) == len(TABLES)
    column_rows = [p for s, p in stmts if "tap_schema.columns" in s]
    assert len(column_rows) == sum(len(t.columns) for t in TABLES)
    key_rows = [p for s, p in stmts if "tap_schema.keys " in s]
    assert len(key_rows) == len(TABLES) - 1  # one FK per child table
    # all registrations are idempotent
    assert all("ON CONFLICT" in s or "WHERE NOT EXISTS" in s for s in inserts)


def test_required_optional_field_is_nullable():
    """`x: str | None` without a default is required by pydantic but accepts
    an explicit null — the column must therefore be nullable."""
    from pydantic import BaseModel

    class Demo(BaseModel):
        demo_id: str
        note: str | None

    (table,) = build_tables(Demo, "s", "demos")
    cols = {c.name: c for c in table.columns}
    assert cols["note"].nullable
    assert not cols["demo_id"].nullable


def test_dict_fields_map_to_jsonb():
    from pydantic import BaseModel

    class Demo(BaseModel):
        demo_id: str
        attrs: dict[str, str] | None = None

    (table,) = build_tables(Demo, "s", "demos")
    cols = {c.name: c for c in table.columns}
    assert cols["attrs"].sql_type == "jsonb"


def test_ddl_migrates_new_model_fields_into_existing_tables():
    """A table created by an older library release gains new columns via
    ADD COLUMN IF NOT EXISTS instead of breaking ingestion."""
    statements = ddl_statements(TABLES, "tap_reader")
    products = next(t for t in TABLES if t.name == "data_products")
    alters = [
        s
        for s in statements
        if s.startswith(f"ALTER TABLE {products.qualified} ADD COLUMN IF NOT EXISTS")
    ]
    non_key = [c for c in products.columns if not c.is_key]
    assert len(alters) == len(non_key)
    # migration-added columns are nullable: never NOT NULL or CHECK in ALTERs
    assert all("NOT NULL" not in s and "CHECK" not in s for s in alters)
    # key columns are structural (part of the PK) and never ALTERed in
    assert not any(f"ADD COLUMN IF NOT EXISTS {products.id_column} " in s for s in alters)
