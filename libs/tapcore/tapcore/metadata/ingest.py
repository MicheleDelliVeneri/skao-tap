"""Generic metadata ingestion for plugin-defined model hierarchies:
schema bootstrap, hierarchical upserts, amendments, document rebuilds.

All functions are parameterized by a :class:`tapcore.metadata.plugins.MetadataPlugin`
— the relational layout is derived from the plugin's pydantic models by
:mod:`tapcore.metadata.schema_gen`, so a new model release that adds fields or
levels changes the database schema and the TAP_SCHEMA registration
automatically (existing tables are migrated forward at startup). Renames are
the one change that is not automatic: additive DDL cannot move rows, so a
domain that moved leaves its old tables behind and startup only warns about
them (see ``_warn_legacy_tables`` and ``scripts/migrate_legacy_tables.sql``).
"""

import datetime
import json
import logging
import typing
from enum import Enum

from psycopg.types.json import Jsonb
from pydantic import BaseModel, TypeAdapter, ValidationError

from ..config import settings
from ..errors import UsageError
from . import regions
from .plugins import MetadataPlugin
from .schema_gen import TableSpec, ddl_statements, registration_statements

# How a derived column's value is computed from its source column's value,
# by SQL type (see ColumnSpec.derived_from in schema_gen). A conversion
# error is the caller's mistake, so it surfaces as UsageError.
DERIVATIONS = {"spoly": regions.stcs_to_spoly}


def _derive(col, source_value):
    try:
        return DERIVATIONS[col.sql_type](source_value)
    except ValueError as exc:
        raise UsageError(str(exc)) from None


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
    _warn_legacy_tables(conn, plugin)


def _warn_legacy_tables(conn, plugin: MetadataPlugin) -> None:
    """Warn while tables from before a domain rename still exist.

    The DDL is additive (CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT
    EXISTS), so a renamed domain leaves its old tables — and their
    TAP_SCHEMA registration and read grant — in place. Rows stranded there
    are invisible to ingest, fetch, list and amend, and survive DELETE,
    while staying queryable over TAP. Cleaning that up drops data, so the
    service only reports it; scripts/migrate_legacy_tables.sql does the work.
    """
    for name in plugin.legacy_tables:
        row = conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()
        if row is None or row[0] is None:
            continue
        log.warning(
            "legacy table %s still exists; the %r domain now serves %s. Rows left"
            " in %s are not served by the API and are not removed by DELETE, yet"
            " remain TAP-queryable — run scripts/migrate_legacy_tables.sql",
            name,
            plugin.name,
            plugin.tables[0].qualified,
            name,
        )


def _log_safe(value: str, limit: int = 200) -> str:
    """Single-line, quoted, length-capped rendering of caller-supplied text."""
    flattened = " ".join(str(value).split())
    if len(flattened) > limit:
        flattened = f"{flattened[:limit]}..."
    return repr(flattened)


def _column_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, dict)):
        try:
            json_value = json.loads(json.dumps(value, default=_json_fallback))
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata value is not JSON serializable") from exc
        return Jsonb(json_value)
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
            if col.derived_from:
                # subscript, not .get(): `row` starts as the key chain and is
                # filled in table.columns order, so a derived column can only
                # be resolved after its source. schema_gen appends the
                # companion immediately after the source column, which holds
                # today — and a KeyError on some future reordering is worth
                # far more than the NULL geometry .get() would have written,
                # since a NULL footprint answers no ADQL query and says nothing.
                row[col.name] = _derive(col, row[col.derived_from])
                continue
            row[col.name] = _resolve_path(instance, col.path)
        for child in tables:
            if child.parent is table:
                children.append((child, getattr(instance, child.field_name, []) or []))
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
    # coerced values and derivations go into a copy: the caller's dict is an
    # argument, not scratch space
    resolved = dict(values)
    derived = {c.name: c for c in table.columns if c.derived_from}
    for column, value in values.items():
        if column not in columns:
            raise UsageError(f"{table_name} has no column {column!r}")
        if column in keys:
            raise UsageError(f"{column} is a key column and cannot be amended")
        if column in derived:
            raise UsageError(
                f"{column} is derived from {derived[column].derived_from};"
                f" amend {derived[column].derived_from} instead"
            )
        info = table.field_for_column(column)
        if info is not None:
            # constraints declared via Field(ge=..., ...) live in
            # FieldInfo.metadata, not in the annotation — re-attach them
            annotation = info.annotation
            if info.metadata:
                annotation = typing.Annotated[annotation, *info.metadata]  # pyright: ignore
            try:
                resolved[column] = TypeAdapter(annotation).validate_python(value)
            except ValidationError as exc:
                errors = "; ".join(e["msg"] for e in exc.errors())
                raise UsageError(f"invalid value for {table_name}.{column}: {errors}") from None
    for column in match:
        if column not in columns:
            raise UsageError(f"{table_name} has no column {column!r} to match on")
    # an amended source column carries its derivations with it: a footprint
    # whose text changed and whose geometry did not would answer ADQL
    # queries with the old sky
    for col in derived.values():
        if col.derived_from in resolved:
            resolved[col.name] = _derive(col, resolved[col.derived_from])

    root_id_column = tables[0].id_column
    conditions = dict(match)
    conditions[root_id_column] = root_id
    sets = ", ".join(f"{c} = %s" for c in resolved)
    where = " AND ".join(f"{c} = %s" for c in conditions)
    cur = conn.execute(
        f"UPDATE {table.qualified} SET {sets} WHERE {where}",
        tuple(_column_value(v) for v in resolved.values())
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
                    doc[child.field_name] = load(child, child_chain)
            # drop inherited key columns, nulls, and service-derived
            # columns (internal query machinery, not document content)
            derived = {c.name for c in table.columns if c.derived_from}
            for key in list(doc):
                if (key in key_chain) or doc[key] is None or key in derived:
                    del doc[key]
            documents.append(_unflatten(doc, table))
        return documents

    docs = load(tables[0], {tables[0].id_column: root_id})
    return docs[0] if docs else None


def delete_document(conn, plugin: MetadataPlugin, root_id: str, actor: str | None = None) -> bool:
    """Delete one root document and all descendants through FK cascades.

    ``actor`` is the authenticated subject responsible, recorded in the audit
    line so a cascading deletion can be traced to a person rather than only
    to a point in time.
    """
    root = plugin.tables[0]
    result = conn.execute(
        f"DELETE FROM {root.qualified} WHERE {root.id_column} = %s",
        (root_id,),
    )
    deleted = result.rowcount > 0
    if deleted:
        # deletion is destructive and cascades: leave an audit trail. The id
        # comes from the request path, so it is quoted and stripped of the
        # newlines that would let a caller forge extra log records.
        log.info(
            "deleted %s %s by %s (cascading to %d descendant table(s))",
            root.qualified,
            _log_safe(root_id),
            _log_safe(actor) if actor else "an unauthenticated caller",
            len(plugin.tables) - 1,
        )
    return deleted


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
    root_derived = {c.name for c in root.columns if c.derived_from}
    summaries = []
    for row in rows:
        doc = {k: v for k, v in row[0].items() if v is not None and k not in root_derived}
        doc = _unflatten(doc, root)
        for table, count in zip(descendants, row[1:], strict=True):
            doc[table.field_name] = count
        summaries.append(doc)
    return summaries
