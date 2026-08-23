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
configured — see the observability guide), and package 9
(horizontal autoscaling: an opt-in CPU HorizontalPodAutoscaler for tap-api and
queue-backlog scaling for tap-executor through a KEDA ScaledObject or a plain
external-metric HPA, with the chart refusing scale-to-zero on a metric the
executor itself exports, a CPU HPA beside a VPA that controls CPU, a CPU
target with no CPU request, and a replica maximum whose pools would exceed
max_connections — see the autoscaling guide), and package 4
(identity and registry: INDIGO IAM bearer tokens verified against the
issuer's JWKS, authorisation behind a plugin with the SRCNet Permissions API
and local IAM groups shipped, seven gateable operations with the query surface
enforced as a group, IVOA AuthVO challenges naming the IAM, job ownership and
attributable deletion, and a VOResource record at /tap/registry — see the
authentication and registry guides).

## Measured findings

A running log of what the benchmark suite
(`benchmarks/tap-performance`, see [Benchmarking](benchmarking.md)) has
established, newest first. Each entry is a measurement rather than an opinion,
so it can be checked and it can go stale — the run that produced it is named.

### 2026-08-23 — the I/O-bound regime is still unmeasured

At both sizes measured so far the buffer cache hit ratio is **100.000%** with
**zero blocks read from disk**: D1 and D2 fit entirely in `shared_buffers` plus
the page cache. Every conclusion below about size therefore describes an
in-memory database, and the point where the working set stops fitting — which
is what `DATABASE_IO_BOUND` exists to catch — has not been reached. D3 (25 GiB)
and D4 (45 GiB) against a 6 GiB PostgreSQL are where that changes, and until
they run the suite has said nothing about I/O.

### 2026-08-23 — a full aggregate scales with the table, as it must

Q13 (`GROUP BY` over all of ObsCore) went from a p95 of 393 ms on D1 to
**3,128 ms on D2** — roughly 8x for 5x the rows — and is the only class that
classifies `DATABASE_CPU_BOUND`. Nothing is wrong with the plan; a full
aggregate is proportional work. It is the class that will make an
`EXPLAIN`-based admission decision worth having, because a user can issue it
synchronously today (see package 11).

### 2026-08-23 — ADQL translation was the service (fixed)

Translation cost **41.5 ms of a ~50 ms synchronous request**, against under
1 ms in PostgreSQL for most query shapes. The single-core ceiling was 24
requests/s and it was set in the parser, not the database. Cause: the parser
library parsed twice per translation and discarded the first tree, and ANTLR
ran full-context prediction (71% of the profile). Fixed by parsing once and
trying SLL prediction first with a fallback — **41.55 ms → 1.18 ms**, verified
identical on all 12,000 corpus queries. End to end on D1, one replica and one
worker: **20.1 → 230.7 requests/s sustainable (11.5x)**, p95 at four clients
77 ms → 28 ms.

### 2026-08-23 — the API is still the constraint, at both sizes

After that fix the normal mix remains `TAP_CPU_BOUND` on D1 (2 GiB) and D2
(10 GiB) alike: PostgreSQL sits near idle for it. Saturation moved from four
concurrent clients on D1 to eight on D2. So replicas and `tapApi.workers`
remain the throughput lever — and each worker is now worth roughly 200
requests/s rather than 20.

### 2026-08-23 — five times the data costs about a fifth of the throughput

D1 (2.06 GiB, 600k ObsCore rows) served 177 requests/s at one client; D2
(10.26 GiB, 3.0M rows) served 145 — **-18% for 5x the data**, with p95
unchanged at 13 ms. Index-assisted queries are close to flat against size, as
they should be; the size sweep continues to D3 and D4, where the working set
stops fitting in memory and this is expected to change character.

### 2026-08-23 — the remaining costs have separated

With parsing no longer dominating, the expensive classes are individually
attributable rather than uniformly slow:

| | p95 on D1 | classified |
| --- | --- | --- |
| normal mix (Q01–Q08, Q10) | 22–43 ms | `TAP_CPU_BOUND` |
| Q11, 10,000-row result | 352 ms | `SERIALIZATION_BOUND` |
| Q13, full aggregate | 393 ms | `DATABASE_CPU_BOUND` |

Those two are the next optimisation targets, and they are unrelated to each
other — see packages 10 and 11.

### 2026-08-23 — cone search needs an index on the *expression*

The translator emits `spoint(RADIANS(s_ra), RADIANS(s_dec)) @ scircle(...)`, a
function applied to the columns, so a GiST index on a stored `spoint` column is
never considered and every cone search becomes a sequential scan. The index has
to be on that expression; `spoint`, `scircle` and `radians` are all
`IMMUTABLE`, so it is legal, and the planner does use it once present. This is
deployment guidance the project did not previously state.

### 2026-08-23 — the planner misjudges join fan-out

Ten plan nodes costing 5 ms or more carry cardinality estimates 50x to 477x
out, all in the join-heavy classes (Q09, Q11, Q14) and none in the normal mix.
Extended statistics on the CAOM parent/child key pairs is the obvious
candidate; the impact is currently confined to the stress classes.

## Package 10 — Cheaper large results

Q11 (10,000 wide ObsCore rows) has a p95 of 352 ms and classifies as
`SERIALIZATION_BOUND`: a busy API and an idle database, so the time is going
into producing bytes rather than finding rows. Worth investigating in order:
where the per-row cost actually is (the typed serialisation path builds a
Python value per cell), whether the VOTable writer or the DSV writer dominates,
and whether Arrow/Parquet — which already stream in record batches — are
materially cheaper per row and should be recommended for bulk transfer.

## Package 11 — Aggregate and scan-bound queries

Q13 (`GROUP BY` over the whole ObsCore table) has a p95 of 393 ms and is the
only class that makes PostgreSQL the constraint. A full aggregate over a large
table is honest work, so the question is not how to make the scan free but
what a service should do about it: parallel query settings for the in-chart
database, whether `EXPLAIN`-based rejection should refuse the most expensive
synchronous queries and steer them to `/async`, and whether summary tables or
materialised views belong in the metadata domains that need them.

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

