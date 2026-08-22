# JSON API (`/api/v1`)

The IVOA TAP endpoints speak the XML the VO standards mandate (VOTable,
UWS, VOSI) so TOPCAT, PyVO and astroquery work out of the box. For
machine-to-machine integration the service also exposes a JSON interface at
`/api/v1`, documented by the live OpenAPI spec at `/openapi.json` (Swagger
UI at `/docs`). Both interfaces share one engine: the same ADQL translator,
TAP_SCHEMA publication checks, MAXREC limits and UWS job store.

## Queries

```
POST /api/v1/query
{"query": "SELECT TOP 5 * FROM ska.continuum_sources", "maxrec": 100}
```

returns `{"metadata": [...], "data": [[...], ...], "status": "OK|OVERFLOW"}`,
with each metadata entry carrying `datatype`, `unit`, `ucd` and
`description` from TAP_SCHEMA. Set `"format": "parquet"` (or `"arrow"`,
`"votable"`, `"csv"`, `"tsv"`) for other output formats — Parquet responses
embed the column metadata in the Arrow schema and the DALI status in the
file metadata.
Errors come back as JSON (`{"error": "UsageError", "message": ...}`) with
the same HTTP status codes as the DALI VOTable errors on `/tap`.

## Jobs (asynchronous)

A JSON facade over the same UWS job store used by `/tap/async` — a job
created here is visible there and vice versa:

| Method & path | Purpose |
|---|---|
| `POST /api/v1/jobs` `{query, lang?, maxrec?, format?, run?}` | Create (and with `run: true` queue) a job; the query is validated up front |
| `GET /api/v1/jobs?phase=COMPLETED,ERROR&last=10` | List jobs |
| `GET /api/v1/jobs/{id}` | Job document (phase, timing, parameters, result/error) |
| `POST /api/v1/jobs/{id}/phase` `{"phase": "RUN"\|"ABORT"}` | Start or abort |
| `GET /api/v1/jobs/{id}/result` | Fetch the result file |
| `DELETE /api/v1/jobs/{id}` | Destroy the job |

## Metadata

`GET /api/v1/tables` returns TAP_SCHEMA as JSON (schemas, tables, columns
with units/UCDs) — the machine-friendly twin of VOSI `/tap/tables`.

## Metadata-domain plugins

Metadata domains are **plugins**: each binds an upstream pydantic model
package to a SQL schema and a mount point, and gets the same endpoint set.
Two ship built in, and third-party model packages register through the
`skao_tap.models` entry-point group — installed alongside the services,
no changes to this codebase. `TAP_MODEL_PLUGINS` (Helm:
`config.modelPlugins`) selects what a deployment activates: `all`, or a
comma-separated subset for dedicated per-model systems.

| Plugin | Model package | SQL schema | Mount |
|---|---|---|---|
| `odp` | [ska-src-mm-notification](https://gitlab.com/ska-telescope/src/src-mm/ska-src-mm-notification) (`Project → … → DataProduct → Artifact`) | `srcnet` | `/api/v1/notifications` |
| `software` | [ska-src-sdm](https://gitlab.com/ska-telescope/src/src-mm/ska-src-mm-software-data-model) (`Software → Artifact`, embedded discovery/resources/provenance) | `software` | `/api/v1/software` |

Every active plugin serves (shown for `odp`; identically for
`/api/v1/software/{uri}` etc.):

| Method & path | Purpose |
|---|---|
| `POST /api/v1/notifications` | Validate (with the plugin's pydantic models) and store a document; idempotent upsert |
| `GET /api/v1/notifications` | Root-level summary of ingested metadata, with per-table row counts |
| `GET /api/v1/notifications/{project_id}` | Reconstruct the full nested document |
| `PATCH /api/v1/notifications/{project_id}` | Amend already-ingested rows: `{"table", "match"?, "values"}` |

Invalid payloads are rejected with HTTP 422 and pydantic's structured
error list — including the library's cross-field rules (`em_min <= em_max`,
image products requiring `s_ra/s_dec/s_fov`, ...).

### Amending ingested metadata

When a data-model release adds a field, existing tables gain the column
automatically at startup (nullable) — `PATCH` then backfills it, or fixes
any stored value, without re-sending whole notifications:

```json
PATCH /api/v1/notifications/project12314
{"table": "data_products",
 "match": {"product_id": "SKAO-19571257111"},
 "values": {"beam_pa": 42.0}}
```

`table` is one of `projects`, `observations`, `scheduling_blocks`,
`execution_blocks`, `data_products`, `artifacts`; `match` narrows the rows
by column equality (omit it to update every row of the project); each
value in `values` is validated against the corresponding pydantic model
field, so amendments obey the same constraints as ingestion. Key columns
cannot be changed. The response reports the number of rows updated.
Re-`POST`ing a full notification remains the way to amend everything at
once (idempotent upsert).

### Model-driven database schema

Each plugin's tables are **generated from its pydantic models at startup**
(`tapcore/schema_gen.py`):

- each `list[Model]` level becomes a child table with a composite primary
  key following the identity chain (`*_id` fields by convention,
  overridable per model class — the software plugin keys on `uri` and
  artifact `location`) and a cascading foreign key;
- singular nested models are flattened into prefixed columns
  (`resources.min_memory` → `resources_min_memory`), reconstructed as
  nested objects on fetch;
- pydantic types map to PostgreSQL types; `Ge/Gt/Le/Lt` constraints and
  enums become `CHECK` constraints, so the database enforces the same
  invariants the library validates;
- every generated table (and key) is registered in `TAP_SCHEMA`, making the
  ingested metadata immediately queryable through TAP/ADQL and the JSON
  query endpoint, and visible in VOSI `/tap/tables`:

```sql
SELECT p.product_id, a.artifact_id, a.access_url
FROM srcnet.data_products AS p
JOIN srcnet.artifacts AS a
  ON  p.project_id = a.project_id
  AND p.eb_id      = a.eb_id
  AND p.product_id = a.product_id
WHERE p.dataproduct_type = 'cube'
```

Upgrading the library to a schema version with new fields adds the new
columns/tables on the next startup (existing columns are never dropped).
