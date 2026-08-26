"""Metadata-domain plugins: pluggable pydantic data models published
through the shared TAP/ADQL and JSON machinery.

A plugin binds an upstream pydantic model hierarchy (e.g. the SKA SRC
notification library, the software discovery model) to a SQL schema, a
JSON API mount point, and identity conventions. Everything downstream —
table generation, TAP_SCHEMA registration, automatic column migration,
the ingest/fetch/amend endpoints — is generic and instantiated once per
active plugin (:mod:`egernia_core.metadata.schema_gen`, :mod:`egernia_core.metadata.ingest`).

Discovery uses the ``egernia.models`` entry-point group, so a third-party
model package makes itself available simply by being installed alongside
the services:

    [project.entry-points."egernia.models"]
    mydomain = "my_package.plugin:PLUGIN"

Per-deployment selection is configuration (``TAP_MODEL_PLUGINS``): ``all``
activates every discovered plugin (one combined archive), a comma-separated
list activates a subset (down to one system per model).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache

from pydantic import BaseModel

from .. import load_entry_points
from ..config import settings
from .schema_gen import TableSpec, build_tables

log = logging.getLogger("egernia_core")

ENTRY_POINT_GROUP = "egernia.models"


@dataclass(frozen=True, eq=False)
class MetadataPlugin:
    """Everything the shared machinery needs to publish one metadata domain."""

    name: str  # selection key in TAP_MODEL_PLUGINS
    model: type[BaseModel]  # root of the pydantic hierarchy
    sql_schema: str  # SQL schema the generated tables live in
    root_table: str  # name of the root table
    description: str  # TAP_SCHEMA schema description
    mount: str  # JSON API mount point: /api/v1/<mount>
    # identity-field overrides per model class name, for hierarchies whose
    # levels have no required '*_id' string field
    id_fields: dict[str, str] = field(default_factory=dict)
    # prepended to child table names; domains sharing a SQL schema use it to
    # keep generic level names (e.g. "artifacts") distinct
    child_table_prefix: str = ""
    # qualified tables this domain used before a rename; startup warns while
    # they still exist, because the API neither reads nor deletes them
    legacy_tables: tuple[str, ...] = ()
    # invoked by ingest.ensure_schema() after this plugin's tables and
    # TAP_SCHEMA registration are ensured — same connection, same
    # transaction, advisory lock still held. This is where a domain builds
    # what its tables imply: the odp plugin derives the ivoa.obscore view.
    post_ensure: Callable[..., None] | None = None

    @property
    def tables(self) -> list[TableSpec]:
        return _tables(self)


# MetadataPlugin is eq=False, so instances hash by identity and cache safely.
@cache
def _tables(plugin: MetadataPlugin) -> list[TableSpec]:
    return build_tables(
        plugin.model,
        plugin.sql_schema,
        plugin.root_table,
        plugin.id_fields or None,
        plugin.child_table_prefix,
    )


@cache
def discovered_plugins() -> dict[str, MetadataPlugin]:
    """All plugins installed in this environment, by name."""
    plugins: dict[str, MetadataPlugin] = {}
    for _, plugin in load_entry_points(
        ENTRY_POINT_GROUP, lambda obj: isinstance(obj, MetadataPlugin), "metadata"
    ):
        if plugin.name in plugins:
            log.error("duplicate metadata plugin name %r; keeping the first", plugin.name)
            continue
        plugins[plugin.name] = plugin
    return plugins


def active_plugins() -> list[MetadataPlugin]:
    """The plugins this deployment activates (TAP_MODEL_PLUGINS)."""
    available = discovered_plugins()
    selection = settings.model_plugins.strip()
    if selection.lower() == "all":
        active = list(available.values())
    else:
        active = []
        selected_names: set[str] = set()
        for name in (part.strip() for part in selection.split(",")):
            if not name:
                continue
            if name in selected_names:
                raise ValueError(f"TAP_MODEL_PLUGINS selects plugin {name!r} more than once")
            if name not in available:
                known = ", ".join(sorted(available)) or "none"
                raise LookupError(
                    f"TAP_MODEL_PLUGINS selects unknown plugin {name!r} (installed: {known})"
                )
            selected_names.add(name)
            active.append(available[name])
    _check_table_collisions(active)
    return active


def _check_table_collisions(plugins: list[MetadataPlugin]) -> None:
    """Two active domains must never generate the same qualified table.

    Uncached on purpose: ``active_plugins`` is called at import/startup time
    only, and the per-plugin ``tables`` are already memoized, so re-running
    the check keeps the settings-driven selection re-readable at no cost.
    """
    owners: dict[str, str] = {}
    for plugin in plugins:
        for table in plugin.tables:
            other = owners.get(table.qualified)
            if other is not None:
                raise ValueError(
                    f"metadata plugins {other!r} and {plugin.name!r} both generate"
                    f" {table.qualified}; set a distinct child_table_prefix,"
                    " root_table or sql_schema"
                )
            owners[table.qualified] = plugin.name
