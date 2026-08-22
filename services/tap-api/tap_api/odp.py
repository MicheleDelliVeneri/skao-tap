"""Observatory data product (ODP) metadata: ingestion of SRC notifications
into the ``srcnet`` schema — bootstrap, hierarchical upserts, amendments.

The relational layout is derived at import time from the
ska-src-mm-notification pydantic models by :mod:`tap_api.schema_gen`, so a
new library release that adds fields or levels changes the database schema
and the TAP_SCHEMA registration automatically. (``srcnet`` stays the SQL
schema name — it is the public, ADQL-facing name existing queries use.)
"""

import json
import logging
from enum import Enum

from psycopg.types.json import Jsonb
from pydantic import BaseModel, TypeAdapter, ValidationError
from ska_src_mm_notification.models.schemas.srcnet_ingestion import SRCIngestionNotification
from tapcore.config import settings
from tapcore.errors import UsageError

from .schema_gen import TableSpec, build_tables, ddl_statements, registration_statements

log = logging.getLogger("tap-api")

SCHEMA = "srcnet"
SCHEMA_DESCRIPTION = (
    "SKA SRC ingestion metadata, generated from the ska-src-mm-notification data model"
)
ADVISORY_LOCK_KEY = 7_412_001  # arbitrary, service-wide

TABLES = build_tables(SRCIngestionNotification, SCHEMA, "projects")


def ensure_schema(conn) -> None:
    """Create the srcnet tables and register them in TAP_SCHEMA (idempotent)."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
    for statement in ddl_statements(TABLES, settings.query_role):
        conn.execute(statement)
    for statement, params in registration_statements(TABLES, SCHEMA_DESCRIPTION):
        conn.execute(statement, params)
    log.info("srcnet schema ensured (%d tables)", len(TABLES))


def _column_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, dict)):
        return Jsonb(json.loads(json.dumps(value)))
    return value


def _upsert(conn, table: TableSpec, row: dict) -> None:
    columns = [c.name for c in table.columns]
    placeholders = ", ".join(["%s"] * len(columns))
    non_pk = [c for c in columns if c not in table.pk_columns]
    conflict = (
        "DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pk)
        if non_pk
        else "DO NOTHING"
    )
    conn.execute(
        f"INSERT INTO {table.qualified} ({', '.join(columns)}) VALUES ({placeholders})"
        f" ON CONFLICT ({', '.join(table.pk_columns)}) {conflict}",
        tuple(_column_value(row.get(c)) for c in columns),
    )


def ingest_notification(conn, notification: SRCIngestionNotification) -> dict[str, int]:
    """Flatten a validated notification into the generated tables (upsert).

    Returns per-table row counts for the response body.
    """
    counts: dict[str, int] = {}
    tables_by_id = {id(t): t for t in TABLES}

    def store(table: TableSpec, instance: BaseModel, key_chain: dict) -> None:
        row = dict(key_chain)
        children: list[tuple[TableSpec, list[BaseModel]]] = []
        for col in table.columns:
            if col.name in row:
                continue
            row[col.name] = getattr(instance, col.name, None)
        for child in tables_by_id.values():
            if child.parent is table:
                children.append((child, getattr(instance, child.name, []) or []))
        _upsert(conn, table, row)
        counts[table.qualified] = counts.get(table.qualified, 0) + 1
        next_chain = dict(key_chain)
        next_chain[table.id_column] = getattr(instance, table.id_column)
        for child, items in children:
            for item in items:
                store(child, item, next_chain)

    store(TABLES[0], notification, {})
    return counts


def amend_rows(conn, project_id: str, table_name: str, match: dict, values: dict) -> int:
    """Partial update of already-ingested rows — e.g. backfilling a column
    added by a newer data-model release without re-sending notifications.

    ``table_name`` is the model-level name (projects, observations, ...,
    artifacts); ``match`` narrows the rows by column equality (empty = all
    rows of the project); ``values`` are the columns to set. Every value is
    validated against the pydantic field it maps to, so amendments obey the
    same constraints as ingestion. Key columns cannot be changed or used
    with wrong names; the update is always scoped to the project.

    Returns the number of rows updated.
    """
    table = next((t for t in TABLES if t.name == table_name), None)
    if table is None:
        names = ", ".join(t.name for t in TABLES)
        raise UsageError(f"unknown table {table_name!r} (one of: {names})")
    columns = {c.name for c in table.columns}
    keys = set(table.pk_columns)

    if not values:
        raise UsageError("values must contain at least one column to set")
    for column, value in values.items():
        if column not in columns:
            raise UsageError(f"{table_name} has no column {column!r}")
        if column in keys:
            raise UsageError(f"{column} is a key column and cannot be amended")
        field = table.model.model_fields.get(column)
        if field is not None:
            try:
                values[column] = TypeAdapter(field.annotation).validate_python(value)
            except ValidationError as exc:
                errors = "; ".join(e["msg"] for e in exc.errors())
                raise UsageError(f"invalid value for {table_name}.{column}: {errors}") from None
    for column in match:
        if column not in columns:
            raise UsageError(f"{table_name} has no column {column!r} to match on")

    conditions = dict(match)
    conditions["project_id"] = project_id
    sets = ", ".join(f"{c} = %s" for c in values)
    where = " AND ".join(f"{c} = %s" for c in conditions)
    cur = conn.execute(
        f"UPDATE {table.qualified} SET {sets} WHERE {where}",
        tuple(_column_value(v) for v in values.values())
        + tuple(_column_value(v) for v in conditions.values()),
    )
    return cur.rowcount


def fetch_notification(conn, project_id: str) -> dict | None:
    """Rebuild the nested notification document for one project."""

    def load(table: TableSpec, key_chain: dict) -> list[dict]:
        where = " AND ".join(f"{k} = %s" for k in key_chain)
        rows = conn.execute(
            f"SELECT to_jsonb(t) FROM {table.qualified} t WHERE {where} ORDER BY {table.id_column}",
            tuple(key_chain.values()),
        ).fetchall()
        documents = []
        for (doc,) in rows:
            child_chain = dict(key_chain)
            child_chain[table.id_column] = doc[table.id_column]
            for child in TABLES:
                if child.parent is table:
                    doc[child.name] = load(child, child_chain)
            # drop inherited key columns and nulls for a clean document
            for key in list(doc):
                if (key in key_chain) or doc[key] is None:
                    del doc[key]
            documents.append(doc)
        return documents

    docs = load(TABLES[0], {"project_id": project_id})
    return docs[0] if docs else None
