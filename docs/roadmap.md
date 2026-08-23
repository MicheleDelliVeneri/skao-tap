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
PITR and restore procedures — see the deployment guide), and package 8
(unified SRCNet logging and observability: `ska-src-logging` in every
service, `X-Request-ID` correlation carried from the API through the job row
and the SQL itself into the executor's records, seven Prometheus metrics
chosen from what recent performance work needed and did not have, an opt-in
in-chart Prometheus for trying them out, and OTLP tracing when a collector is
configured — see the observability guide), and package 4
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

## Package 9 — Horizontal autoscaling — *current*

Package 8 exported the queue backlog and called it "what to autoscale
executors on"; this package is the consumer.

- **tap-api on CPU**: an opt-in HorizontalPodAutoscaler on CPU utilisation,
  which needs nothing beyond metrics-server. CPU is the honest signal for a
  service whose work is GIL-held ANTLR translation.
- **tap-executor on the queue**: `tap_oldest_queued_job_seconds` divided by
  a configured seconds-of-backlog-per-replica, through a KEDA `ScaledObject`
  by default (the PromQL then ships with the chart) or a plain HPA on an
  external metric for clusters already serving one.
- **Refuse the configurations that do not work**: scale-to-zero on a metric
  the executor itself exports, a CPU HPA beside a VPA in `Auto`, a CPU target
  with no CPU request, and a maximum replica count whose pools would exceed
  `postgresql.tuning.max_connections`.
- **Still to do**: per-identity quotas so one user cannot occupy every
  executor a scaler adds, and `EXPLAIN`-based rejection of synchronous
  queries too expensive to run at all — autoscaling answers "not enough
  capacity", not "this query should never have been accepted".

