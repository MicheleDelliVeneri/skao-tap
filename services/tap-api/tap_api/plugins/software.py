"""Software discovery metadata plugin.

Binds the ska-src-sdm (SKA Software Discovery Metadata) model to the
``software`` SQL schema and the ``/api/v1/software`` mount. The model's
identity conventions differ from the notification library — the root is
keyed by ``uri`` ({publisher}:{name}:{version}) and artifacts by their
``location`` — declared here as identity overrides; its singular nested
objects (discovery, resources, provenance, ...) are flattened into
prefixed columns by the shared schema generator.
"""

from ska_src_sdm import Software
from tapcore.metadata.plugins import MetadataPlugin

PLUGIN = MetadataPlugin(
    name="software",
    model=Software,
    sql_schema="software",
    root_table="software",
    description=("SKA SRC software discovery metadata, generated from the ska-src-sdm data model"),
    mount="software",
    id_fields={"Software": "uri", "Artifact": "location"},
)
