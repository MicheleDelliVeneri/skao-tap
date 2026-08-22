# Roadmap

Follow-up work is organized in numbered packages, referenced by number in
issues, PRs and discussions. Package numbers are stable: delivered packages
are removed from this page but their numbers are not reused.

Delivered and no longer tracked here: package 1 (typed, streaming result
pipeline with Parquet/Arrow output), package 2 (TAP table upload:
inline multipart and http(s) `UPLOAD` on /sync and /async, per-query
`TAP_UPLOAD` temp tables, `uploadMethods`/`uploadLimit` in capabilities,
configurable limits), and package 3 (UWS completeness: `WAIT` blocking
requests on the job and phase resources, `AFTER` job-list filtering, and
real `ABORT` that cancels the executing statement via the backend PID and
`pg_cancel_backend()`).

## Package 4 — Identity and registry — *current*

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
