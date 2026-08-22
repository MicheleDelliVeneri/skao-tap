"""Software discovery metadata plugin.

Binds the ska-src-sdm (SKA Software Discovery Metadata) model to the
``srcnet.software`` table and the ``/api/v1/software`` mount. The model's
identity conventions differ from the notification library — the root is
keyed by ``uri`` ({publisher}:{name}:{version}) and artifacts by their
``location`` — declared here as identity overrides; its singular nested
objects (discovery, resources, provenance, ...) are flattened into
prefixed columns by the shared schema generator.
"""

# pyright: reportMissingImports=false

from ska_src_sdm import Software
from tapcore.metadata.plugins import MetadataPlugin

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
)
