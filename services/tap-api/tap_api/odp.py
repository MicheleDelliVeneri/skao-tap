"""Observatory data product (ODP) metadata plugin.

Binds the ska-src-mm-notification pydantic models to the ``srcnet`` SQL
schema (its public, ADQL-facing name) and the ``/api/v1/notifications``
mount. All machinery is the shared, model-driven pipeline in
:mod:`tapcore.schema_gen` and :mod:`tapcore.ingest` — a new library
release that adds fields or levels changes the database schema and the
TAP_SCHEMA registration automatically.
"""

from ska_src_mm_notification.models.schemas.srcnet_ingestion import SRCIngestionNotification
from tapcore import ingest
from tapcore.plugins import MetadataPlugin

PLUGIN = MetadataPlugin(
    name="odp",
    model=SRCIngestionNotification,
    sql_schema="srcnet",
    root_table="projects",
    description=(
        "SKA SRC ingestion metadata, generated from the ska-src-mm-notification data model"
    ),
    mount="notifications",
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
