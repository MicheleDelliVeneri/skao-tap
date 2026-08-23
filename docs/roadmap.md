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
`pg_cancel_backend()`), and package 6 (plugin-based metadata databases:
domains bind a pydantic model package to a SQL schema and mount point via
a small contract, the shared machinery in `tapcore.metadata`
does the rest, third-party packages register through the
`skao_tap.models` entry-point group, and `TAP_MODEL_PLUGINS` selects what
a deployment activates; the ODP/srcnet and software-discovery domains
ship built in — the user data product domain follows when its model
package exists), and package 5 (scaling, resilience and backup: default
soft anti-affinity/zone-spread and PodDisruptionBudgets for multi-replica
services, opt-in VerticalPodAutoscalers per service, `postgresql.tuning`
server arguments for right-sizing the in-chart database, a scheduled
`pg_dump` backup CronJob with retention, and documented HA-PostgreSQL,
PITR and restore procedures — see the deployment guide), and package 4
(identity and registry: INDIGO IAM bearer tokens verified against the
issuer's JWKS, authorisation behind a plugin with the SRCNet Permissions API
and local IAM groups shipped, seven gateable operations with the query surface
enforced as a group, IVOA AuthVO challenges naming the IAM, job ownership and
attributable deletion, and a VOResource record at /tap/registry — see the
authentication and registry guides).

## Package 7 — Queryable region footprints (`s_region`) — *blocked*

Waiting on `ska-src-mm-notification` 0.1.8, which adds the STC-S validator
this depends on; the upstream change is raised.

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

## Package 8 — Unified SRCNet logging and observability — *current*

The services currently use ad-hoc `logging.getLogger("tap_api")` /
`"tapcore"` loggers with the default formatting, so their output does not
join up with the rest of SRCNet.

- **Adopt `ska-src-logging`**: replace the local logger setup with the
  shared
  [SKA SRC API logging library](https://gitlab.com/ska-telescope/src/src-api/ska-src-api-logging)
  (`get_logger(app_name=...)`) in `tapcore`, `tap-api` and `tap-executor`,
  so every record carries the standard structured fields and JSON output
  in deployments (colourised console locally).
- **Request correlation**: propagate `X-Request-ID` across tap-api →
  PostgreSQL → tap-executor so one user query, its UWS job and the
  executor's statements share a correlation id; wrap per-job work in the
  library's `LogContext` to attach `job_id`, `owner_id` and the metadata
  domain to every record in scope.
- **OpenTelemetry and metrics**: `setup_uvicorn_logging()` in the FastAPI
  lifespan, `setup_otel_fastapi(app, service_name=...)` for traces and log
  shipping, and `setup_metrics_endpoint(app)` for Prometheus scraping —
  mounted so it does not collide with the TAP/VOSI resource paths, and
  exposed through the Helm chart (endpoint, service annotations,
  opt-out for air-gapped sites).
- **Redaction**: use the library's sensitive-data redaction on the paths
  that handle tokens and user-supplied identifiers, keeping the log-safe
  rendering already applied to metadata ids.
- **Consistency pass**: audit existing log statements for level and
  message shape while porting, and cover the correlation-id propagation
  with a component test.
