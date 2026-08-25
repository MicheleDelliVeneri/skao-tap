# Metadata plugins

The service publishes **metadata domains** — bodies of structured metadata
defined by a pydantic data model — through one shared, model-driven
pipeline. Each domain is a *plugin*: it declares its model and where it
lives, and the framework does everything else.

Two domains ship built in:

| Plugin | Model package | Tables | JSON mount |
| --- | --- | --- | --- |
| `odp` | [ska-src-mm-notification](https://gitlab.com/ska-telescope/src/src-mm/ska-src-mm-notification) — observatory data products (`Project → Observation → SchedulingBlock → ExecutionBlock → DataProduct → Artifact`) | `srcnet.projects`, …, `srcnet.data_products`, `srcnet.artifacts` | `/api/v1/notifications` |
| `software` | [ska-src-sdm](https://gitlab.com/ska-telescope/src/src-mm/ska-src-mm-software-data-model) — software discovery (`Software → Artifact`, with embedded discovery/resources/provenance objects) | `srcnet.software`, `srcnet.software_artifacts` | `/api/v1/software` |

All SRC metadata lives in the one **`srcnet`** SQL schema, distinguished
by table name — so a future user-data-product domain lands as
`srcnet.user_data_products`, and ADQL queries can join freely across
domains. Domains sharing the schema set `child_table_prefix` to keep
generic level names distinct (`artifacts` belongs to the ODP hierarchy,
so the software domain's artifacts are `srcnet.software_artifacts`);
overlapping table names are rejected at startup rather than silently
colliding.

Third-party model packages join the same way, without changing this
codebase (see [Writing a plugin](#writing-a-plugin)).

## What a plugin gets for free

From the model alone, the framework derives and maintains (the complete,
automatically generated column reference is available on the
[Generated model schemas](model-schemas.md) page):

- **Relational tables** — every `list[Model]` level becomes a child table
  with a composite primary key following the identity chain and a
  cascading foreign key; singular nested models are flattened into
  prefixed columns (`resources.min_memory` → `resources_min_memory`);
  pydantic constraints and enums become `CHECK` constraints.
- **TAP_SCHEMA registration** — tables, columns and keys, so ingested
  metadata is immediately queryable through ADQL and visible in VOSI
  `/tap/tables`.
- **Automatic migration** — a newer model release that adds fields gains
  the columns at startup (`ADD COLUMN IF NOT EXISTS`, nullable); nothing
  is ever dropped. Renames are the exception: additive DDL cannot move
  rows, so a domain that changes schema or table name declares the old
  names in `legacy_tables` and startup warns until the one-off
  [migration](development.md#upgrading-an-existing-deployment) is run.
- **A JSON endpoint set** under `/api/v1/<mount>`: `POST` (validate and
  upsert), `GET` (root summary with per-table counts), `GET /{id}`
  (nested document), `PATCH /{id}` (amend stored rows), and `DELETE /{id}`
  (delete the root document and cascade through its child rows).

## Selecting plugins per deployment

`TAP_MODEL_PLUGINS` (Helm: `config.modelPlugins`) chooses which of the
*installed* plugins a deployment activates:

| Value | Result |
| --- | --- |
| `all` (default) | One combined archive serving every installed domain |
| `odp` | Only the observatory data product domain |
| `odp,software` | An explicit subset |

Only active plugins are bootstrapped, registered in TAP_SCHEMA and
mounted, so the same container image serves a single-domain deployment or
a combined one — a fleet of per-model systems is the same chart with
different values.

## Writing a plugin

A plugin is a `MetadataPlugin` value plus an entry point. In your own
package:

```python
# my_package/plugin.py
from tapcore.metadata.plugins import MetadataPlugin

from my_package.models import MyRootModel

PLUGIN = MetadataPlugin(
    name="mydomain",  # selection key in TAP_MODEL_PLUGINS
    model=MyRootModel,  # root of the pydantic hierarchy
    sql_schema="srcnet",  # SQL schema for the generated tables
    root_table="things",  # name of the root table
    description="My metadata domain",  # TAP_SCHEMA schema description
    mount="mydomain",  # JSON API mount: /api/v1/mydomain
    # optional: identity fields for models without a required '*_id' field
    id_fields={"MyRootModel": "uri"},
    # optional: prefix for child tables, so generic level names stay unique
    # within the shared schema (srcnet.things_parts, not srcnet.parts)
    child_table_prefix="things_",
    # optional: qualified tables this domain used before a rename; startup
    # warns while they still hold rows the API no longer serves or deletes
    legacy_tables=("mydomain.things",),
)
```

```toml
# my_package/pyproject.toml
[project.entry-points."skao_tap.models"]
mydomain = "my_package.plugin:PLUGIN"
```

Install it alongside the services (in the image, or as an extra
dependency) and it is discovered automatically:

```console
$ pip install my-package
$ TAP_MODEL_PLUGINS=all uvicorn tap_api.main:app
INFO  active metadata plugins: odp, software, mydomain
```

### Model requirements

- **Identity fields.** Each level needs a stable identifier. By
  convention that is a required `str` field ending in `_id`; when a model
  uses something else, name it in `id_fields` (the software plugin keys
  `Software` on `uri` and `Artifact` on `location`). Primary keys are the
  chain of identifiers from the root down, so children are unique within
  their parent.
- **Supported field types.** `str`, `int`, `float`, `bool`, `datetime`,
  `Enum` (becomes `text` + a `CHECK`), lists/dicts of scalars (`jsonb`),
  singular nested models (flattened), and `list[Model]` (child table).
  Unsupported annotations are skipped rather than failing the build.
- **Constraints carry over.** `Field(ge=…, le=…, …)` becomes a database
  `CHECK`, and the same constraint is re-applied when amending a column
  via `PATCH` — including for flattened columns.

## Amending ingested metadata

`PATCH /api/v1/<mount>/{root_id}` updates stored rows without re-sending a
whole document — the usual case being a column added by a newer model
release:

```json
PATCH /api/v1/software/ska:dsc-037-delay-ps:0.1.3
{"table": "software", "values": {"status": "DEPRECATED"}}
```

`table` is a model-level table name, `match` (optional) narrows rows by
column equality, and every value in `values` is validated against the
corresponding pydantic field. Key columns cannot be changed, and the
update is always scoped to the given root document. Re-`POST`ing a full
document remains the way to amend everything at once.

## Querying footprints (`s_region`)

Data products and artifacts carry their sky footprint as an STC-S string —
`CIRCLE`, `POLYGON` or `POSITION`, ICRS frame, degrees — validated at the
producer by the data-model package. Text is not queryable, so at ingestion
the service derives a pgsphere geometry into a companion column,
`s_region_geom`, indexes it with GiST, and registers it in TAP_SCHEMA. ADQL
`INTERSECTS` and `CONTAINS` accept it wherever a region goes:

```sql
-- everything overlapping a half-degree cone
SELECT product_id, s_region
FROM srcnet.data_products
WHERE 1 = INTERSECTS(s_region_geom, CIRCLE('ICRS', 150.0, -30.0, 0.5))

-- everything whose footprint covers a position
SELECT product_id
FROM srcnet.data_products
WHERE 1 = CONTAINS(POINT('ICRS', 150.1, -30.1), s_region_geom)
```

The conversion is shape-faithful: a `POLYGON`'s vertices are used verbatim,
a `CIRCLE` becomes a 32-vertex inscribed polygon (area within 0.7%), and a
`POSITION` becomes a 0.1-arcsecond-wide octagon so point footprints answer
overlap queries as points — small enough to stay within SKA astrometry, and
wide enough that its adjacent vertices sit ~186x clear of pgsphere's 1e-9 rad
coordinate epsilon, which the half-milliarcsecond polygon this started with
did not. Amending `s_region` through `PATCH` re-derives the geometry in the
same update, and a string that is not a valid region is
refused with a 400 — at ingestion by the model's validator, at amendment by
the service. The derived column never appears in fetched documents; it
exists for ADQL.

## Deleting ingested metadata

`DELETE /api/v1/<mount>/{root_id}` removes the root document. Every generated
child-table foreign key uses `ON DELETE CASCADE`, so deleting a project also
removes its observations, scheduling/execution blocks, data products, and
artifacts; deleting software likewise removes `srcnet.software_artifacts`.
Unknown identifiers return HTTP 404.

## Querying across domains

The runnable [`demo/srcnet_metadata_tap.ipynb`](https://github.com/MicheleDelliVeneri/skao-tap/blob/main/demo/srcnet_metadata_tap.ipynb)
populates 100 positioned rows in `srcnet.data_products` plus
`srcnet.software` against the Docker Compose deployment, then demonstrates
PyVO discovery, spatial and asynchronous queries, amendment, and deletion.

Because every plugin registers in TAP_SCHEMA, all domains are queryable
through the same ADQL endpoints — including joins across them when the
metadata relates:

```sql
SELECT s.uri, s.status, a.location
FROM srcnet.software AS s
JOIN srcnet.software_artifacts AS a ON a.uri = s.uri
WHERE a.kind = 'DOCKER'
```

Note that `TAP_MODEL_PLUGINS` governs what a deployment *bootstraps and
mounts*, not what ADQL can read: tables already present in the database
stay queryable. Genuine per-domain isolation means separate databases —
which is exactly what a one-system-per-model topology gives you.

## Where the code lives

| Path | Role |
| --- | --- |
| `libs/tapcore/tapcore/metadata/plugins.py` | The `MetadataPlugin` contract, entry-point discovery, deployment selection |
| `libs/tapcore/tapcore/metadata/schema_gen.py` | Models → tables, constraints, TAP_SCHEMA registration, migrations |
| `libs/tapcore/tapcore/metadata/ingest.py` | Generic ingest / fetch / list / amend |
| `services/tap-api/tap_api/plugins/` | The built-in plugin definitions (`odp.py`, `software.py`) |
| `services/tap-api/tap_api/endpoints/json_api.py` | Router factory mounting one endpoint set per active plugin |
