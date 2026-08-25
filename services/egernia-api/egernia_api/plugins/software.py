"""Software discovery metadata plugin.

Binds the ska-src-sdm (SKA Software Discovery Metadata) model to the
``srcnet.software`` table and the ``/api/v1/software`` mount. The model's
identity conventions differ from the notification library — the root is
keyed by ``uri`` ({publisher}:{name}:{version}) and artifacts by their
``location`` — declared here as identity overrides; its singular nested
objects (discovery, resources, provenance, ...) are flattened into
prefixed columns by the shared schema generator.
"""

from egernia_core.metadata.plugins import MetadataPlugin
from ska_src_sdm import Software

PLUGIN = MetadataPlugin(
    name="software",
    model=Software,
    sql_schema="srcnet",
    root_table="software",
    description="SKA SRC metadata, generated from the SRC data models",
    mount="software",
    id_fields={"Software": "uri", "Artifact": "location"},
    # shares the srcnet schema with the other domains: srcnet.software,
    # srcnet.software_artifacts (ODP already owns srcnet.artifacts)
    child_table_prefix="software_",
    # pre-0.2 layout, before the move into the shared srcnet schema; a
    # deployment upgraded across that move keeps these tables (and their
    # rows) until scripts/migrate_legacy_tables.sql is run
    legacy_tables=("software.software", "software.artifacts"),
)
