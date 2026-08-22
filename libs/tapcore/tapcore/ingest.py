"""Generic metadata ingestion for plugin-defined model hierarchies:
schema bootstrap, hierarchical upserts, amendments, document rebuilds.

All functions are parameterized by a :class:`tapcore.plugins.MetadataPlugin`
— the relational layout is derived from the plugin's pydantic models by
:mod:`tapcore.schema_gen`, so a new model release that adds fields or
levels changes the database schema and the TAP_SCHEMA registration
automatically (existing tables are migrated forward at startup).
"""

import datetime
import json
import logging
import typing
from enum import Enum

from psycopg.types.json import Jsonb
from pydantic import BaseModel, TypeAdapter, ValidationError

from .config import settings
from .errors import UsageError
from .plugins import MetadataPlugin
from .schema_gen import TableSpec, ddl_statements, registration_statements

log = logging.getLogger("tapcore")

ADVISORY_LOCK_KEY = 7_412_001  # arbitrary, service-wide


def ensure_schema(conn, plugin: MetadataPlugin) -> None:
    """Create the plugin's tables and register them in TAP_SCHEMA (idempotent)."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
    tables = plugin.tables
    for statement in ddl_statements(tables, settings.query_role):
        conn.execute(statement)
    for statement, params in registration_statements(tables, plugin.description):
        conn.execute(statement, params)
    log.info("%s schema ensured (%d tables)", plugin.sql_schema, len(tables))


def _column_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, dict)):
        return Jsonb(json.loads(json.dumps(value, default=_json_fallback)))
    return value


def _json_fallback(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return str(value)


def _resolve_path(instance: BaseModel, path: tuple[str, ...]):
    """Value at an attribute path; None as soon as any step is absent
    (a flattened embedded object may be missing entirely)."""
    value = instance
    for step in path:
        if value is None:
            return None
        value = getattr(value, step, None)
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


def ingest_document(conn, plugin: MetadataPlugin, document: BaseModel) -> dict[str, int]:
    """Flatten a validated model instance into the generated tables (upsert).

    Returns per-table row counts for the response body.
    """
    tables = plugin.tables
    counts: dict[str, int] = {}

    def store(table: TableSpec, instance: BaseModel, key_chain: dict) -> None:
        row = dict(key_chain)
        children: list[tuple[TableSpec, list[BaseModel]]] = []
        for col in table.columns:
            if col.name in row:
                continue
            row[col.name] = _resolve_path(instance, col.path)
        for child in tables:
            if child.parent is table:
                children.append((child, getattr(instance, child.name, []) or []))
        _upsert(conn, table, row)
        counts[table.qualified] = counts.get(table.qualified, 0) + 1
        next_chain = dict(key_chain)
        next_chain[table.id_column] = getattr(instance, table.id_column)
        for child, items in children:
            for item in items:
                store(child, item, next_chain)

    store(tables[0], document, {})
    return counts


def amend_rows(
    conn, plugin: MetadataPlugin, root_id: str, table_name: str, match: dict, values: dict
) -> int:
    """Partial update of already-ingested rows — e.g. backfilling a column
    added by a newer data-model release without re-sending documents.

    ``table_name`` is the model-level name; ``match`` narrows the rows by
    column equality (empty = all rows of the root document); ``values`` are
    the columns to set. Every value is validated against the pydantic field
    it maps to (through flattening), so amendments obey the same
    constraints as ingestion. Key columns cannot be changed; the update is
    always scoped to the root document.

    Returns the number of rows updated.
    """
    tables = plugin.tables
    table = next((t for t in tables if t.name == table_name), None)
    if table is None:
        names = ", ".join(t.name for t in tables)
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
        info = table.field_for_column(column)
        if info is not None:
            # constraints declared via Field(ge=..., ...) live in
            # FieldInfo.metadata, not in the annotation — re-attach them
            annotation = info.annotation
            if info.metadata:
                annotation = typing.Annotated[annotation, *info.metadata]
            try:
                values[column] = TypeAdapter(annotation).validate_python(value)
            except ValidationError as exc:
                errors = "; ".join(e["msg"] for e in exc.errors())
                raise UsageError(f"invalid value for {table_name}.{column}: {errors}") from None
    for column in match:
        if column not in columns:
            raise UsageError(f"{table_name} has no column {column!r} to match on")

    root_id_column = tables[0].id_column
    conditions = dict(match)
    conditions[root_id_column] = root_id
    sets = ", ".join(f"{c} = %s" for c in values)
    where = " AND ".join(f"{c} = %s" for c in conditions)
    cur = conn.execute(
        f"UPDATE {table.qualified} SET {sets} WHERE {where}",
        tuple(_column_value(v) for v in values.values())
        + tuple(_column_value(v) for v in conditions.values()),
    )
    return cur.rowcount


def _unflatten(doc: dict, table: TableSpec) -> dict:
    """Rebuild embedded objects out of their flattened columns."""
    for col in table.columns:
        if len(col.path) <= 1 or col.name not in doc:
            continue
        value = doc.pop(col.name)
        if value is None:
            continue
        target = doc
        for step in col.path[:-1]:
            target = target.setdefault(step, {})
        target[col.path[-1]] = value
    return doc


def fetch_document(conn, plugin: MetadataPlugin, root_id: str) -> dict | None:
    """Rebuild the nested document for one root identifier."""
    tables = plugin.tables

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
            for child in tables:
                if child.parent is table:
                    doc[child.name] = load(child, child_chain)
            # drop inherited key columns and nulls for a clean document
            for key in list(doc):
                if (key in key_chain) or doc[key] is None:
                    del doc[key]
            documents.append(_unflatten(doc, table))
        return documents

    docs = load(tables[0], {tables[0].id_column: root_id})
    return docs[0] if docs else None


def list_documents(conn, plugin: MetadataPlugin) -> list[dict]:
    """Root-level summary: every root row plus per-descendant-table counts."""
    tables = plugin.tables
    root = tables[0]
    descendants = [t for t in tables if t.parent is not None]
    count_selects = "".join(
        f", (SELECT count(*) FROM {t.qualified} c WHERE c.{root.id_column} = p.{root.id_column})"
        for t in descendants
    )
    rows = conn.execute(
        f"SELECT to_jsonb(p){count_selects} FROM {root.qualified} p ORDER BY p.{root.id_column}"
    ).fetchall()
    summaries = []
    for row in rows:
        doc = {k: v for k, v in row[0].items() if v is not None}
        doc = _unflatten(doc, root)
        for table, count in zip(descendants, row[1:], strict=True):
            doc[table.name] = count
        summaries.append(doc)
    return summaries
