"""Automatic relational schema generation from pydantic data models.

A metadata-domain model hierarchy (see :mod:`tapcore.metadata.plugins`) is walked
generically:

- every ``list[BaseModel]`` field becomes a child table named after the field;
- a singular nested ``BaseModel`` field is flattened into prefixed columns
  (``resources.min_memory`` -> ``resources_min_memory``), recursively;
- scalar fields become columns (str->text, int->bigint, float->double
  precision, bool->boolean, datetime->timestamptz, Enum->text + CHECK,
  list[scalar]->jsonb);
- numeric Ge/Gt/Le/Lt constraints on the pydantic fields become CHECKs, so
  the database enforces the same invariants the model validates;
- each table's primary key is the chain of identity fields down the
  hierarchy — a required ``*_id`` string field by default, overridable per
  model class for hierarchies with different identity conventions — with a
  foreign key to the parent;
- every generated table is registered in TAP_SCHEMA (including keys), which
  makes the ingested metadata immediately queryable through TAP/ADQL and
  visible in the VOSI /tables document.

The DDL is idempotent (CREATE IF NOT EXISTS / ON CONFLICT) and existing
deployments are migrated forward for new model fields with ADD COLUMN IF
NOT EXISTS; it is applied at tap-api startup under an advisory lock.
"""

import datetime
import types
import typing
from dataclasses import dataclass, field
from enum import Enum

import annotated_types
from pydantic import BaseModel
from pydantic.fields import FieldInfo

SQL_TYPES = {
    str: "text",
    int: "bigint",
    float: "double precision",
    bool: "boolean",
    datetime.datetime: "timestamptz",
}
TAP_DATATYPES = {
    "text": ("char", "*"),
    "bigint": ("long", None),
    "double precision": ("double", None),
    "boolean": ("boolean", None),
    "jsonb": ("char", "*"),
    "timestamptz": ("char", "*"),
}


@dataclass
class ColumnSpec:
    name: str
    sql_type: str
    nullable: bool
    checks: list[str] = field(default_factory=list)
    description: str | None = None
    is_key: bool = False
    # attribute path from the model instance to the value; longer than one
    # element for columns flattened out of singular nested models
    path: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.path:
            self.path = (self.name,)


@dataclass
class TableSpec:
    schema: str
    name: str
    model: type[BaseModel]
    parent: TableSpec | None
    id_column: str
    columns: list[ColumnSpec] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def pk_columns(self) -> list[str]:
        chain: list[str] = []
        if self.parent is not None:
            chain.extend(self.parent.pk_columns)
        chain.append(self.id_column)
        return chain

    def field_for_column(self, name: str) -> FieldInfo | None:
        """The pydantic field a column maps to (through flattening)."""
        for col in self.columns:
            if col.name != name:
                continue
            model = self.model
            info = None
            for step in col.path:
                if model is None:
                    return None
                info = model.model_fields.get(step)
                if info is None:
                    return None
                base, _ = _unwrap(info.annotation)
                model = base if isinstance(base, type) and issubclass(base, BaseModel) else None
            return info
        return None


def _unwrap(annotation, collected: list | None = None):
    """Strip Optional/Annotated wrappers; return (type, nullable).

    When ``collected`` is given, metadata found on Annotated layers is
    appended to it — including the metadata of nested FieldInfo objects
    produced by the ``Optional[Annotated[T, Field(...)]]`` pattern.
    """
    nullable = False
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            args = typing.get_args(annotation)
            if collected is not None:
                for meta in args[1:]:
                    if isinstance(meta, FieldInfo):
                        collected.extend(meta.metadata)
                        if meta.description:
                            collected.append(meta)
                    else:
                        collected.append(meta)
            annotation = args[0]
        elif origin in (typing.Union, types.UnionType):
            args = [a for a in typing.get_args(annotation) if a is not type(None)]
            nullable = True
            if len(args) != 1:
                return None, nullable
            annotation = args[0]
        else:
            return annotation, nullable


def _child_model(annotation) -> type[BaseModel] | None:
    """Return the item model when the annotation is list[BaseModel]."""
    base, _ = _unwrap(annotation)
    if typing.get_origin(base) is list:
        item, _ = _unwrap(typing.get_args(base)[0])
        if isinstance(item, type) and issubclass(item, BaseModel):
            return item
    return None


def _nested_model(annotation) -> type[BaseModel] | None:
    """Return the model when the annotation is a singular nested BaseModel."""
    base, _ = _unwrap(annotation)
    if isinstance(base, type) and issubclass(base, BaseModel):
        return base
    return None


def _numeric_checks(name: str, metadata: list) -> list[str]:
    checks = []
    for meta in metadata:
        if isinstance(meta, annotated_types.Ge):
            checks.append(f"{name} >= {meta.ge}")
        elif isinstance(meta, annotated_types.Gt):
            checks.append(f"{name} > {meta.gt}")
        elif isinstance(meta, annotated_types.Le):
            checks.append(f"{name} <= {meta.le}")
        elif isinstance(meta, annotated_types.Lt):
            checks.append(f"{name} < {meta.lt}")
    return checks


def _identity_field(model: type[BaseModel], overrides: dict[str, str] | None) -> str:
    if overrides and model.__name__ in overrides:
        name = overrides[model.__name__]
        if name not in model.model_fields:
            raise ValueError(f"{model.__name__} has no field {name!r} (identity override)")
        return name
    for name, info in model.model_fields.items():
        base, _ = _unwrap(info.annotation)
        if name.endswith("_id") and base is str and info.is_required():
            return name
    raise ValueError(
        f"{model.__name__} has no required '*_id' identity field"
        " (declare one in the plugin's id_fields)"
    )


def _scalar_column(
    name: str, info: FieldInfo, force_not_null: bool, path: tuple[str, ...]
) -> ColumnSpec | None:
    collected: list = list(info.metadata)
    base, nullable = _unwrap(info.annotation, collected)
    if base is None:
        return None
    description = info.description or next(
        (m.description for m in collected if isinstance(m, FieldInfo)), None
    )
    checks: list[str] = []
    if isinstance(base, type) and issubclass(base, Enum):
        values = ", ".join(f"'{v.value}'" for v in base)
        checks.append(f"{name} IN ({values})")
        sql_type = "text"
    elif base in SQL_TYPES:
        sql_type = SQL_TYPES[base]
        if sql_type in ("bigint", "double precision"):
            checks.extend(_numeric_checks(name, collected))
    elif typing.get_origin(base) in (list, dict) or base in (list, dict):
        sql_type = "jsonb"
    else:
        return None
    return ColumnSpec(
        name=name,
        sql_type=sql_type,
        nullable=nullable and not force_not_null,
        checks=checks,
        description=description,
        path=path,
    )


def _flatten(model: type[BaseModel], prefix: str, path: tuple[str, ...]) -> list[ColumnSpec]:
    """Columns for a singular nested model, prefixed with its field name."""
    columns: list[ColumnSpec] = []
    for fname, info in model.model_fields.items():
        if _child_model(info.annotation) is not None:
            continue  # list-of-model children inside embedded models: unsupported
        nested = _nested_model(info.annotation)
        if nested is not None:
            columns.extend(_flatten(nested, f"{prefix}_{fname}", (*path, fname)))
            continue
        # built under the flattened name so CHECK constraints reference the
        # actual column, not the model-level field name
        column = _scalar_column(
            f"{prefix}_{fname}", info, force_not_null=False, path=(*path, fname)
        )
        if column is not None:
            column.nullable = True  # the whole embedded object may be absent
            columns.append(column)
    return columns


def build_tables(
    root_model: type[BaseModel],
    schema: str,
    root_table: str,
    id_overrides: dict[str, str] | None = None,
) -> list[TableSpec]:
    """Walk the model hierarchy and produce table specs in creation order."""
    tables: list[TableSpec] = []

    def walk(model: type[BaseModel], name: str, parent: TableSpec | None) -> None:
        table = TableSpec(
            schema=schema,
            name=name,
            model=model,
            parent=parent,
            id_column=_identity_field(model, id_overrides),
        )
        # inherited key columns first, own identity column included naturally
        for key in table.pk_columns[:-1]:
            table.columns.append(ColumnSpec(name=key, sql_type="text", nullable=False, is_key=True))
        children: list[tuple[str, type[BaseModel]]] = []
        for fname, info in model.model_fields.items():
            child = _child_model(info.annotation)
            if child is not None:
                children.append((fname, child))
                continue
            nested = _nested_model(info.annotation)
            if nested is not None:
                table.columns.extend(_flatten(nested, fname, (fname,)))
                continue
            column = _scalar_column(fname, info, force_not_null=fname == table.id_column, path=())
            if column is not None:
                if fname == table.id_column:
                    column.nullable = False
                    column.is_key = True
                table.columns.append(column)
        tables.append(table)
        for fname, child in children:
            walk(child, fname, table)

    walk(root_model, root_table, None)
    return tables


def ddl_statements(tables: list[TableSpec], query_role: str) -> list[str]:
    """Idempotent DDL for the generated tables plus read grants.

    Existing deployments are migrated forward automatically for the common
    model evolution — new fields: after each CREATE TABLE IF NOT EXISTS, an
    ADD COLUMN IF NOT EXISTS is emitted per column, so a table created by an
    older model release gains the new columns without losing any stored
    metadata. Columns added this way are nullable regardless of the model
    (existing rows predate the field; the pydantic layer still validates new
    payloads), and columns dropped from the model are left in place.
    """
    schema = tables[0].schema
    statements = [f"CREATE SCHEMA IF NOT EXISTS {schema}"]
    for table in tables:
        lines = []
        for col in table.columns:
            null = "" if col.nullable else " NOT NULL"
            lines.append(f"    {col.name} {col.sql_type}{null}")
        for col in table.columns:
            for check in col.checks:
                lines.append(f"    CHECK ({check})")
        lines.append(f"    PRIMARY KEY ({', '.join(table.pk_columns)})")
        if table.parent is not None:
            pk = ", ".join(table.parent.pk_columns)
            lines.append(
                f"    FOREIGN KEY ({pk}) REFERENCES {table.parent.qualified} ({pk})"
                " ON DELETE CASCADE"
            )
        body = ",\n".join(lines)
        statements.append(f"CREATE TABLE IF NOT EXISTS {table.qualified} (\n{body}\n)")
        statements.extend(
            f"ALTER TABLE {table.qualified} ADD COLUMN IF NOT EXISTS {col.name} {col.sql_type}"
            for col in table.columns
            if not col.is_key
        )
    statements.append(f"GRANT USAGE ON SCHEMA {schema} TO {query_role}")
    statements.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {query_role}")
    return statements


def _tap_datatype(sql_type: str) -> tuple[str, str | None]:
    return TAP_DATATYPES[sql_type]


def registration_statements(
    tables: list[TableSpec], schema_description: str
) -> list[tuple[str, tuple]]:
    """Parameterized inserts registering the generated tables in TAP_SCHEMA."""
    schema = tables[0].schema
    stmts: list[tuple[str, tuple]] = [
        (
            "INSERT INTO tap_schema.schemas (schema_name, description, schema_index)"
            " VALUES (%s, %s, 100) ON CONFLICT (schema_name) DO UPDATE"
            " SET description = EXCLUDED.description",
            (schema, schema_description),
        )
    ]
    for t_index, table in enumerate(tables, start=1):
        stmts.append(
            (
                "INSERT INTO tap_schema.tables (schema_name, table_name, table_type,"
                " description, table_index) VALUES (%s, %s, 'table', %s, %s)"
                " ON CONFLICT (table_name) DO UPDATE SET description = EXCLUDED.description",
                (schema, table.qualified, table.model.__doc__ or table.name, t_index),
            )
        )
        for c_index, col in enumerate(table.columns, start=1):
            datatype, arraysize = _tap_datatype(col.sql_type)
            stmts.append(
                (
                    "INSERT INTO tap_schema.columns (table_name, column_name, datatype,"
                    " arraysize, description, indexed, principal, std, column_index)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s)"
                    " ON CONFLICT (table_name, column_name) DO UPDATE"
                    " SET description = EXCLUDED.description",
                    (
                        table.qualified,
                        col.name,
                        datatype,
                        arraysize,
                        col.description or col.name,
                        1 if col.is_key else 0,
                        1 if col.is_key or not col.nullable else 0,
                        c_index,
                    ),
                )
            )
        if table.parent is not None:
            key_id = f"{table.qualified}->{table.parent.qualified}"
            stmts.append(
                (
                    "INSERT INTO tap_schema.keys (key_id, from_table, target_table,"
                    " description) VALUES (%s, %s, %s, %s) ON CONFLICT (key_id) DO NOTHING",
                    (key_id, table.qualified, table.parent.qualified, "hierarchy link"),
                )
            )
            for col_name in table.parent.pk_columns:
                stmts.append(
                    (
                        "INSERT INTO tap_schema.key_columns (key_id, from_column,"
                        " target_column) SELECT %s, %s, %s WHERE NOT EXISTS ("
                        "SELECT 1 FROM tap_schema.key_columns WHERE key_id = %s"
                        " AND from_column = %s)",
                        (key_id, col_name, col_name, key_id, col_name),
                    )
                )
    return stmts
