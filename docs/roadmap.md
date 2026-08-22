# Roadmap

Follow-up work is organized in numbered packages, referenced by number in
issues, PRs and discussions. Package numbers are stable: delivered packages
are removed from this page but their numbers are not reused.

Package 1 (typed, streaming result pipeline with Parquet/Arrow output) is
delivered and no longer tracked here.

## Package 2 — TAP table upload (`UPLOAD`) — *current*

- Inline uploads (multipart VOTable) and `http`/`https` URI uploads.
- Per-job `TAP_UPLOAD` temporary tables; ADQL translation and the
  published-table check made upload-aware.
- `uploadMethods` declared in capabilities; upload limits configurable.

## Package 3 — UWS completeness

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
