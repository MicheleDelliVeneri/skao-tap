"""SRCNet notification ingestion: schema bootstrap and hierarchical upserts.

The relational layout is derived at import time from the
ska-src-mm-notification pydantic models by :mod:`tap_api.schema_gen`, so a
new library release that adds fields or levels changes the database schema
and the TAP_SCHEMA registration automatically.
"""

import json
import logging
from enum import Enum

from psycopg.types.json import Jsonb
from pydantic import BaseModel
from ska_src_mm_notification.models.schemas.srcnet_ingestion import SRCIngestionNotification
from tapcore.config import settings

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
