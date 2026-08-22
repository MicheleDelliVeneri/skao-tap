# Roadmap

Follow-up work is organized in numbered packages, referenced by number in
issues, PRs and discussions. Package numbers are stable: delivered packages
are removed from this page but their numbers are not reused.

Delivered and no longer tracked here: package 1 (typed, streaming result
pipeline with Parquet/Arrow output) and package 2 (TAP table upload:
inline multipart and http(s) `UPLOAD` on /sync and /async, per-query
`TAP_UPLOAD` temp tables, `uploadMethods`/`uploadLimit` in capabilities,
configurable limits).

## Package 3 — UWS completeness — *current*

- `WAIT` blocking requests (UWS 1.1) on the job and phase resources.
- `AFTER` filtering in the job list.
- Real `ABORT`: store the executing backend PID in `uws.jobs` and cancel
  the running statement with `pg_cancel_backend()`.

## Package 4 — Identity and registry

- Authentication and job ownership (`ownerId`), per-user visibility of the
  job list; groundwork for per-user schemas and quotas.
- VOResource record and VO Registry registration of the service.

## Package 5 — Scaling, resilience and backup

- **Vertical autoscaling of the service pods**: VerticalPodAutoscaler
  policies for tap-api and tap-executor (recommendation mode first, then
  auto), with sensible min/max bounds in the Helm chart alongside the
  existing resource requests/limits.
- **Database vertical scaling**: right-size the PostgreSQL StatefulSet from
  observed load (connections, shared_buffers, work_mem for large ADQL
  sorts/joins), expose the knobs as chart values, and document guidance for
  managed/external databases.
- **Distributed deployment for resilience**: multiple tap-api and
  tap-executor replicas spread across nodes/zones (topology spread
  constraints and pod anti-affinity), PodDisruptionBudgets, and a
  highly-available PostgreSQL option (e.g. a streaming-replication operator
  such as CloudNativePG or Zalando) with automated failover. The executor's
  `FOR UPDATE SKIP LOCKED` claim already makes multi-replica execution safe.
- **Backup strategy**: scheduled base backups plus WAL archiving for
  point-in-time recovery of the database (jobs, TAP_SCHEMA, srcnet
  metadata), snapshot or object-storage backup of the results volume with a
  retention policy aligned to `TAP_JOB_RETENTION`, and documented,
  regularly exercised restore procedures.

## Package 6 — Further metadata databases

Beyond the observatory data product metadata (the `srcnet` schema,
generated from ska-src-mm-notification), publish additional metadata
domains through the same TAP/ADQL and JSON machinery:

- **Software database**: catalogue of software (pipelines, containers,
  versions, provenance) available to and used by SRCNet processing.
- **User data product database**: metadata for user-generated data
  products, as opposed to observatory-generated ones.
- Each domain will likely ship its own upstream data-model package (not
  the notification library), so generalize the model-driven pipeline —
  `tap_api.schema_gen` (pydantic models → tables + TAP_SCHEMA
  registration), the ingestion/amendment endpoints, and the automatic
  column migration — to register multiple model packages, each mapping to
  its own SQL schema.

## Package 7 — Queryable region footprints (`s_region`)

The notification model carries `s_region` (STC-S/pgsphere-style strings,
e.g. `CIRCLE 3.5867 -30.4 0.25`) on data products and artifacts, but the
generated schema stores it as plain text — so ObsCore-style footprint
queries (`INTERSECTS(s_region, CIRCLE('ICRS', ...))`) do not work on the
ingested metadata.

- **Parse at ingestion**: convert the STC-S string into a companion
  pgsphere geometry column (`s_region_geom spoly`; circles converted to
  polygon approximations), register it in TAP_SCHEMA, and index it with
  GiST so ADQL `INTERSECTS`/`CONTAINS` over footprints are fast.
- **ska-src-mm-notification 0.1.8 — fix the region type mismatch**: the
  model declares `s_region: str | None` with no format validation, while
  the field's own description promises "pgsphere format or STC-S in ICRS
  frame" — any string (e.g. `"NOT A REGION"`) validates today, unlike the
  numeric fields, which carry Ge/Le constraints. The 0.1.8 release should
  add a pydantic validator for the STC-S grammar (`CIRCLE`, `POLYGON`,
  `POSITION` in ICRS, sensible coordinate ranges), so malformed regions
  are rejected at the producer, before they reach any archive.
- **Reject malformed regions at the API boundary**: until the 0.1.8
  validator lands upstream, the ingestion endpoint should validate the
  region syntax itself (and keep doing so afterwards as defense in depth).
- **Amendments follow**: `PATCH` updates to `s_region` re-derive the
  geometry column.
