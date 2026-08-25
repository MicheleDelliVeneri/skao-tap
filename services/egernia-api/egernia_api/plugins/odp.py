"""Observatory data product (ODP) metadata plugin.

Binds the ska-src-mm-notification pydantic models to the ``srcnet`` SQL
schema (its public, ADQL-facing name) and the ``/api/v1/notifications``
mount. All machinery is the shared, model-driven pipeline in
:mod:`egernia_core.metadata.schema_gen` and :mod:`egernia_core.metadata.ingest` — a new library
release that adds fields or levels changes the database schema and the
TAP_SCHEMA registration automatically.
"""

from egernia_core.metadata import ingest
from egernia_core.metadata.plugins import MetadataPlugin
from ska_src_mm_notification.models.schemas.srcnet_ingestion import SRCIngestionNotification

from . import obscore

PLUGIN = MetadataPlugin(
    name="odp",
    model=SRCIngestionNotification,
    sql_schema="srcnet",
    root_table="projects",
    description="SKA SRC metadata, generated from the SRC data models",
    mount="notifications",
    # the ODP model is ObsCore-derived, so this domain also publishes the
    # standard ivoa.obscore view over its tables
    post_ensure=obscore.ensure_obscore,
)

# Backwards-compatible module-level API (pre-plugin callers and tests).
TABLES = PLUGIN.tables


def ensure_schema(conn) -> None:
    ingest.ensure_schema(conn, PLUGIN)


def ingest_notification(conn, notification: SRCIngestionNotification) -> dict[str, int]:
    return ingest.ingest_document(conn, PLUGIN, notification)


def amend_rows(conn, project_id: str, table_name: str, match: dict, values: dict) -> int:
    return ingest.amend_rows(conn, PLUGIN, project_id, table_name, match, values)


def fetch_notification(conn, project_id: str) -> dict | None:
    return ingest.fetch_document(conn, PLUGIN, project_id)
