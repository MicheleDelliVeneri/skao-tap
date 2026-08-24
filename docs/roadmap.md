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

Like delivered packages, findings whose fixes have shipped and been verified
are removed from this page; git history keeps them, and the runs that
produced them remain in `benchmarks/tap-performance/results/`.

### 2026-08-24 — the four service-side answers shipped

Not a measurement — the response to what the autoscaling and fixed-scaling
families found (runs `20260824T074332Z-b4fa9d64-keda`,
`20260824T102832Z-29507cbb-keda`, `20260824T014320Z-a5058118-fixed-scaling`),
so the log says what changed as well as what was found:

- **The saturating signal is replaced.** The autoscaler read
  `max(tap_oldest_queued_job_seconds)/60` — the age of the queue's head,
  which tops out near one job's service time as soon as the queue drains at
  all. The measured worst case: **1,713 jobs queued, the oldest 54 s old,
  one pod asked for**; every overload profile got three pods out of a
  permitted eight. Retuning cannot fix a signal that saturates, so the
  chart's ScaledObject and the external-metric path now scale on queue
  *depth*, `tap_jobs{phase="QUEUED"}`, against `queuedJobsPerReplica`
  (default 10 ≈ 5 s of queue at ~2 jobs/s per executor). The old
  seconds-denominated values are refused with a message naming the
  replacements, and an empty queue reports depth 0 rather than letting the
  series vanish. The age gauge stays exported as a latency figure.
- **The executor's CPU limit stops lying.** One executor is one thread
  running one job at a time; under sustained load it pins at 0.95–0.97 of a
  core with zero CFS throttling, so its former 2-core limit was headroom
  that cannot exist. The benchmark deployment's limit is now 1, and the
  chart documents the one-core ceiling. Replicas — via the depth-based
  autoscaler — are the executor's scaling axis.
- **Routing becomes measurable.** Every API response carries `X-Served-By`
  (the pod name), so which replica served a request is stated rather than
  inferred — see the scale-out entry below for why that matters.
- **Overload can refuse instead of resetting.** `tapApi.backlog` sizes the
  accept queue and `tapApi.limitConcurrency` makes uvicorn answer 503 past
  a per-worker connection ceiling — off by default until the reset onset
  (below) is established.

### 2026-08-24 — scale-out cost is detection and routing, never the pod

Run `20260824T102832Z-29507cbb-keda`, all seven autoscaling scenarios, six
valid. Provisioning a Pod — created, scheduled, started, Ready — took **zero
to one second in every scenario**, while total scale-out was 246 to 497 s:
detection 98–340 s (the threshold, the polling interval, and the age signal's
own reluctance — since replaced) and **routing 83–243 s** from Ready to
demonstrably serving. Kubernetes is not the delay and there is no point
tuning it.

The routing number is the open question: it was measured by proxy (the first
successful request completing after Ready), and on a deep queue that proxy
may be reporting the queue rather than the routing. Confirm before
attacking — responses now carry `X-Served-By`, so the next family can
measure it directly.

### 2026-08-24 — a third of the shed load is a reset, not a refusal, wherever the pool tips

Run `20260824T014320Z-a5058118-fixed-scaling`. At every point where the
connection pool tipped over, a large fraction of the load was dropped at the
socket rather than refused with an answer:

| measurement | requests | 503 | ReadError | ReadTimeout |
| --- | --- | --- | --- | --- |
| 1 replica, 1×C1 | 4,194 | 1,330 | 893 | 586 |
| 2 replicas, 2×C1 | 15,211 | 2,594 | 11,968 | 540 |
| 4 replicas, 6×C1 | 12,421 | 7,294 | 4,012 | 753 |

The 503s are the pool-timeout path working as designed. The `ReadError`s are
not: a reset connection is indistinguishable from a crash to the client that
receives it, and a client cannot retry it safely. First hypothesis is the
listen socket's accept queue overflowing before the application sees the
connection. What cannot be said from this family is the offered rate at
which resets start, because every measurement that shows them abandoned most
of its arrivals at the generator's in-flight cap; pinning the onset needs a
bounded-concurrency run held just past pool saturation. The service now
exposes `tapApi.backlog` and `tapApi.limitConcurrency` so the shedding can
be made honest once the onset is known.

### 2026-08-24 — replica scaling remains unmeasured

Run `20260824T014320Z-a5058118-fixed-scaling`: seventeen of 24 measurements
were generator-capped, and the seven valid points do not bracket a ceiling
at any replica count — one replica cannot serve C1 (it sheds 98.7% of it),
and two, four and eight replicas all serve 1×C1 with headroom to spare. The
suite nearly published "replica scaling efficiency at 8: 0.25" from two
rates the service met in full; efficiency figures now require a bracketed
capacity. To measure the real thing the ladder needs rungs between 1× and
2×C1 — where the interesting region for two replicas and up turned out to
be — or bounded-concurrency runs per replica count.

### 2026-08-24 — two analysis artefacts are recorded, not yet fixed

Both need a service-image change and were deferred to keep comparability
with the runs already measured; the next image change can carry them.
`peak_pool_wait_p95_s` reads ~9.7 s against a 5 s pool timeout because the
pool-wait histogram's last finite bucket is 10 s, so every timed-out acquire
interpolates to the middle of (5, 10]. And `CONNECTION_POOL_BOUND` takes
confidence `min(1.0, pool_wait)`, so any wait over a second is full
confidence and the class outranks everything else wherever the pool waited
at all.

### 2026-08-24 — the aggregate query is the case for admission control

Q13 (`GROUP BY` over the whole ObsCore table) across the three sizes, four
concurrent clients:

| | p95 | throughput |
| --- | --- | --- |
| D1, 2 GiB | 393 ms | 11.2 requests/s |
| D2, 10 GiB | 3,128 ms | 1.7 requests/s |
| **D3, 25 GiB** | **17,753 ms** | **0.2 requests/s** |

Forty-five times the latency for twelve times the data, and at D3 it is
`DATABASE_IO_BOUND` with a plan that discards 616,550 rows after reading them.
Nothing here is a bad plan — a full aggregate over 7.4 million wide rows is
proportional work — but a user can issue this *synchronously* today, and one
such request occupies a connection for eighteen seconds. That is what makes
package 11's `EXPLAIN`-based admission decision worth building: the service
should be able to recognise this shape and route it to `/async` rather than
hold a synchronous connection for it.

### 2026-08-24 — D3 is where the working set stops fitting

D3 (25.28 GiB, 7.4M ObsCore rows) against a 6 GiB PostgreSQL is the first size
that touches the disk in earnest. Per 180-second measurement window, on client
backends only:

| | buffer hit ratio | blocks read | read wait |
| --- | --- | --- | --- |
| D1 (2 GiB), warm | 100.00% | 0 | 0 s |
| D2 (10 GiB), warm | 100.00% | 0–7 | 0 s |
| **D3 (25 GiB)** | **68–70%** | **1.6–2.2M** | **14–52 s** |

Throughput at one client falls from 145 requests/s on D2 to 115–142 on D3 —
less than a 70% hit ratio might suggest, because the reads are NVMe-fast and a
single client cannot queue behind itself. What this regime costs under
concurrency is the question the rest of the D3 sweep answers.

The first repetition at each size is measurably colder than the rest (D1's
first window read 280 MiB, D2's first read 1,091 MiB, and both were at 100%
thereafter), so the 60-second warmup does not fully warm a working set of this
size. Worth widening the warmup for the larger datasets rather than reading the
first repetition as a result.

### 2026-08-24 — cross-size comparisons made before the corpus fix are not size effects

The corpus used to be rebuilt per dataset tier, so each size was measured
with a different set of queries and the provenance hash described only the
last one built. It is built once per run now, sized to the smallest tier —
but every cross-*size* throughput comparison taken before the fix, including
the published "throughput versus database size" and the earlier "five times
the data costs about a fifth of the throughput" claim, compares different
workloads and should not be read as a size effect. Everything measured
*within* one dataset stands: saturation points, bottleneck classifications,
cache hit ratios, per-class latencies.

### 2026-08-23 — the API is still the constraint, at both sizes

After the translation fix the normal mix remains `TAP_CPU_BOUND` on D1 (2 GiB)
and D2 (10 GiB) alike: PostgreSQL sits near idle for it. Saturation moved from
four concurrent clients on D1 to eight on D2. So replicas and `tapApi.workers`
remain the throughput lever — and each worker is now worth roughly 200
requests/s rather than 20.

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

## Package 7 — Queryable region footprints (`s_region`)

Unblocked: `ska-src-mm-notification` 0.1.8 is released and ships the STC-S
validator this depends on, so the work can start. **First step is the version
bump** — `services/tap-api/pyproject.toml` still pins `>=0.1.7`, so nothing
sees the validator until that becomes `>=0.1.8` and `uv.lock` is refreshed.

The notification model carries `s_region` (STC-S/pgsphere-style strings,
e.g. `CIRCLE 3.5867 -30.4 0.25`) on data products and artifacts, but the
generated schema stores it as plain text — so ObsCore-style footprint
queries (`INTERSECTS(s_region, CIRCLE('ICRS', ...))`) do not work on the
ingested metadata.

- **Parse at ingestion**: convert the STC-S string into a companion
  pgsphere geometry column (`s_region_geom spoly`; circles converted to
  polygon approximations), register it in TAP_SCHEMA, and index it with
  GiST so ADQL `INTERSECTS`/`CONTAINS` over footprints are fast.
- **Take the 0.1.8 validator** *(upstream, done — needs the pin bump)*: the
  region type mismatch is fixed. `0.1.7` declared `s_region: str | None`
  with no format validation, so any string (e.g. `"NOT A REGION"`) validated,
  unlike the numeric fields with their Ge/Le constraints. `0.1.8` adds
  `models/regions.py` with `validate_s_region()` — the `CIRCLE`, `POLYGON`
  and `POSITION` STC-S subset in ICRS with coordinate-range checks — wired
  as a model validator on `BaseNotificationModel`, so malformed regions are
  now rejected at the producer, before they reach any archive.
- **Reject malformed regions at the API boundary**: keep validating region
  syntax at the ingestion endpoint as defence in depth, since a producer can
  always be running an older model package than the service.
- **Amendments follow**: `PATCH` updates to `s_region` re-derive the
  geometry column.

## Package 12 — ObsCore 1.1 compliance (`ivoa.obscore` view)

The service is not ObsCore 1.1 compliant today, and the gap was measured
against the REC (REC-ObsCore-v1.1-20170509) rather than assumed: there is no
`ivoa.ObsCore` table or view in the deployed service (the only literal
`ivoa.obscore` in the repo is the synthetic benchmark table in
`benchmarks/tap-performance/tapbench/dataset/schema.sql`, which is not part of
the service and misses the `*_xel` columns); `/tap/capabilities` and the
VOResource record carry no
`<dataModel ivo-id="ivo://ivoa.net/std/ObsCore#core-1.1">` element
(`services/tap-api/tap_api/endpoints/vosi.py::_capability_elements()`); and
utype/xtype metadata is absent end-to-end — never written by
`tapcore/metadata/schema_gen.py::registration_statements()` (which also
hard-codes `std = 0`), never emitted by `tables_xml()` or the JSON `/tables`
mirror — even though the TAP_SCHEMA DDL already has the columns for it.

The distance is short because the SRCNet ODP model is itself
ObsCore-1.1-derived: `srcnet.data_products` already carries ~18 of the
mandatory columns under their exact ObsCore names (including all five 1.1
axis-length additions `s_xel1/s_xel2/t_xel/em_xel/pol_xel`),
`srcnet.artifacts` has `access_url/access_format/access_estsize`, and
`srcnet.observations` has `collection/facility_name/instrument_name`. The
publication gate (`queries/query.py::_published_tables()`) is a name lookup
in `tap_schema.tables`, so a view passes with no query-path change.

The shape of the fix is a registered **view**, created by the odp plugin's
bootstrap so it exists exactly when its source tables do:

- **`services/tap-api/tap_api/plugins/obscore.py` (new)** — the 29 mandatory
  columns as data (name, datatype, arraysize, xtype, unit, ucd, utype
  transcribed from REC Table 6, `std = 1`, `principal = 1`), the view DDL,
  and `ensure_obscore(conn)` which runs `CREATE SCHEMA IF NOT EXISTS ivoa`,
  `DROP VIEW IF EXISTS` + `CREATE VIEW ivoa.obscore` (drop-and-create so a
  mapping change migrates forward; atomic inside the bootstrap transaction),
  upserts the TAP_SCHEMA registration (schema/table/columns, table utype
  `ivo://ivoa.net/std/ObsCore#core-1.1`), and grants
  `USAGE`/`SELECT` on schema `ivoa` to the query role at runtime — the same
  pattern `srcnet` uses; `db/init/05_roles.sql` stays untouched.
- **View mapping** — one row per data product (the product already aggregates
  its artifacts' axes), joining `srcnet.data_products` to
  `srcnet.observations` and picking one representative artifact for the
  access columns via `LEFT JOIN LATERAL ... WHERE semantics = 'science'
  ORDER BY artifact_id LIMIT 1` (NULL `access_url` is spec-legal — the
  DataLink pattern). Decided mappings: `dataproduct_type = 'table'` (in the
  srcnet CHECK but not the ObsCore vocabulary) maps to `'measurements'` via
  `CASE`; `obs_collection = COALESCE(observations.collection,
  'unclassified')` to honour NOT NULL; `obs_publisher_did` is a configurable
  prefix plus the PK chain
  `<prefix><project_id>/<obs_id>/<sbd_id>/<eb_id>/<product_id>` — new
  setting `TAP_OBSCORE_DID_PREFIX` (Helm `obscore.didPrefix`, default
  `ivo://skao.int/~?`, prefix validated before SQL-literal interpolation; the
  authority must match the registry `authorityId` in a real deployment since
  a PublisherDID is a permanent promise); `access_estsize =
  round(bytes/1000.0)::bigint` (kbyte); `s_resolution` from `beam_size`;
  `t_resolution` and `em_res_power` NULL (permitted); `s_region` is
  registered with `xtype = 'adql:REGION'` now even though the stored value
  is a plain STC-S string — region *functions* over it arrive with
  Package 7, whose geometry column this view inherits.
- **Bootstrap wiring** — an optional `post_ensure` hook on `MetadataPlugin`
  (`libs/tapcore/tapcore/metadata/plugins.py`), invoked by
  `ingest.ensure_schema()` after the plugin's tables and registration are
  ensured, same connection and transaction, advisory xact lock still held
  (concurrent pods safe); `odp.py` wires
  `post_ensure=obscore.ensure_obscore`. `main.py::lifespan` already calls
  `forget_published_tables()` after bootstrap, so the view is immediately
  queryable. The `software` plugin is unaffected — the hook lives only on
  odp.
- **Capability declaration** — `_capability_elements()` gains an
  `_obscore_active()` flag keyed off the active plugins and, when true,
  emits `<dataModel ivo-id="ivo://ivoa.net/std/ObsCore#core-1.1">
  ObsCore-1.1</dataModel>` after `</interface>` and before `<language>`
  (TAPRegExt element order). `voresource_xml()` reuses the same block, so
  the registry record inherits it — and the REC's rule that the identifier
  may only be declared once the table with all mandatory columns exists
  holds structurally, because the flag and the view key off the same plugin.
- **utype/xtype plumbing** — `tables_xml()` additionally selects and emits
  table `utype`, column `utype`, and column `xtype` as the `extendedType`
  attribute on `<dataType>` (VODataService order); the JSON `/tables` in
  `endpoints/json_api.py` adds the same fields. Optional follow-up:
  `tapcore/query/results.py::tap_schema_metadata()` carrying utype/xtype
  into result VOTable FIELDs.
- **Tests** — unit: the column list is exactly the 29 mandatory names with
  spot-checked units/ucds/utypes and the view SQL contains the CASE,
  COALESCE and lateral-pick clauses; capabilities contain the `dataModel`
  element with odp active and not otherwise. Component: `/tables` lists
  `ivoa.obscore` and pyvo sees the metadata; ingest a notification through
  `/api/v1/notifications`, then `SELECT * FROM ivoa.ObsCore` over TAP sync
  returns one row per product with the constructed DID (ADQL case folding
  makes the REC's case-insensitive `ivoa.ObsCore` work unquoted). Optional
  external check: stilts `taplint` against the composed stack.
- **Extras** — an ObsCore cone-search entry in `/tap/examples` gated on the
  same flag, and a `docs/obscore.md` recording the column mapping and the
  DID scheme.

