"""Metadata-domain plugins: pluggable pydantic data models published
through the shared TAP/ADQL and JSON machinery.

A plugin binds an upstream pydantic model hierarchy (e.g. the SKA SRC
notification library, the software discovery model) to a SQL schema, a
JSON API mount point, and identity conventions. Everything downstream —
table generation, TAP_SCHEMA registration, automatic column migration,
the ingest/fetch/amend endpoints — is generic and instantiated once per
active plugin (:mod:`tapcore.metadata.schema_gen`, :mod:`tapcore.metadata.ingest`).

Discovery uses the ``skao_tap.models`` entry-point group, so a third-party
model package makes itself available simply by being installed alongside
the services:

    [project.entry-points."skao_tap.models"]
    mydomain = "my_package.plugin:PLUGIN"

Per-deployment selection is configuration (``TAP_MODEL_PLUGINS``): ``all``
activates every discovered plugin (one combined archive), a comma-separated
list activates a subset (down to one system per model).
"""

import logging
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import entry_points

from pydantic import BaseModel

from ..config import settings
from .schema_gen import TableSpec, build_tables

log = logging.getLogger("tapcore")

ENTRY_POINT_GROUP = "skao_tap.models"


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

    @property
    def tables(self) -> list[TableSpec]:
        cached = _TABLES_CACHE.get(self.name)
        if cached is None:
            cached = build_tables(
                self.model, self.sql_schema, self.root_table, self.id_fields or None
            )
            _TABLES_CACHE[self.name] = cached
        return cached


_TABLES_CACHE: dict[str, list[TableSpec]] = {}


@cache
def discovered_plugins() -> dict[str, MetadataPlugin]:
    """All plugins installed in this environment, by name."""
    plugins: dict[str, MetadataPlugin] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            plugin = entry.load()
        except Exception:
            log.exception("failed to load metadata plugin %r", entry.name)
            continue
        if not isinstance(plugin, MetadataPlugin):
            log.error("entry point %r is not a MetadataPlugin; ignoring", entry.name)
            continue
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
        return list(available.values())
    active = []
    for name in (part.strip() for part in selection.split(",")):
        if not name:
            continue
        if name not in available:
            known = ", ".join(sorted(available)) or "none"
            raise LookupError(
                f"TAP_MODEL_PLUGINS selects unknown plugin {name!r} (installed: {known})"
            )
        active.append(available[name])
    return active
