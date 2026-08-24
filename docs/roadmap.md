# Roadmap

Follow-up work is organized in numbered packages, referenced by number in
issues, PRs and discussions. Package numbers are stable: delivered packages
are removed from this page but their numbers are not reused.

## Measured findings

A running log of what the benchmark suite
(`benchmarks/tap-performance`, see [Benchmarking](benchmarking.md)) has
established, newest first. Each entry is a measurement rather than an opinion,
so it can be checked and it can go stale — the run that produced it is named.

Like delivered packages, findings whose fixes have shipped and been verified
are removed from this page; git history keeps them, and the runs that
produced them remain in `benchmarks/tap-performance/results/`.

### 2026-08-24 — the depth signal verified: the fleet the queue implies, in seconds

Run `20260824T140134Z-bf7e4b24-keda`: the same seven scenarios, first run on
the depth-based trigger (`max(tap_jobs{phase="QUEUED"})`, threshold 10 jobs
per replica, max 8) with the executor's CPU limit at 1. Same build across the
run; the dataset store had grown to 25.45 GiB under the D1 label, so the
regime is I/O-influenced and absolute numbers are directional against the
old family, not build-to-build. The shape is not directional, and the shape
is the verdict:

| | peak pods | mean | max queued | detect | p95 | errors |
| --- | --- | --- | --- | --- | --- | --- |
| K1 idle to 0.5×C1 | 1 | 1.0 | 7 | — | 1.3 s | 0.00% |
| K2 step to 3.5×C1 | **8** | 3.8 | 402 | 12 s | 36.9 s | 0.05% |
| K3 spike to 6×C1 | **8** | 6.2 | 553 | 8 s | 128 s | 0.01% |
| K4 ramp to 6×C1 ⚠ | **8** | 4.8 | 179 | 62 s | 141 s | 0.01% |
| K5 alternating 0.5×/4×C1 | **8** | 6.1 | 297 | 12 s | 21.7 s | 0.02% |
| K6 sustained 6×C1 ⚠ | **8** | 6.6 | 150 | 10 s | 220 s | 0.04% |
| K7 4×C1 to 0.2×C1 | **8** | 4.1 | 320 | 0 s | 17.8 s | 0.05% |

Against the age signal's family: every overload profile now reaches the full
permitted fleet where before every one capped at three; **detection fell
from 98–340 s to 8–12 s** (K4's 62 s is the ramp itself — on a gradual ramp
the queue takes a minute to reach ten jobs, which is the signal being honest,
not slow); queues that grew to 1,700–4,100 jobs are held at **150–553**,
drained as they form; and the errors that reached 9.8–17.3% on the spike and
the sustained overload are **at or under 0.05% in every scenario**. p95 on
the worst profiles: 946 → 128 s (K3), 1,203 → 220 s (K6), 315 → 36.9 s (K2),
327 → 17.8 s (K7). K1, the control, still refuses to scale.

**The routing question is closed.** On pods whose full lifecycle was
captured, Ready-to-serving measured **3.6 s (K4) and 10.5 s (K5)** — the old
family's 83–243 s "routing" stage was the queue wearing the proxy's clothes,
exactly as suspected. Kubernetes provisioning remains 0–1 s. There is
nothing to attack.

The residual p95 in K3/K4/K6 is capacity, not signal: eight pods serve
~22 jobs/s against ~41 offered at 6×C1, so the wall is `maxReplicas` — an
operator's explicit budget, which is where the wall belongs.

Qualifications, in the log's own spirit: K4 abandoned 3.3% and K6 13.4% of
their arrivals at the generator's in-flight cap, so both are qualified as
measurements of their offered rate (a sustained overload is not measurable
open-loop — a property of the scenario, recorded before). The per-scenario
guards were initially dropped because the orchestrator ran a pre-fix
analysis build; `tapbench reclassify` re-derived them from the stored
artefacts, which is what it exists for. And the bottleneck verdicts read
`UNKNOWN` on the maxed-out scenarios because the executor-CPU rule compares
the fleet's usage against its *peak* ready count over the whole window — an
autoscaled fleet ramps, so the ceiling is overstated mid-ramp; package 15.

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
  inferred. The verification run above then closed the question: the old
  83–243 s "routing" stage was the queue; Ready-to-serving is 3.6–10.5 s.
- **Overload can refuse instead of resetting.** `tapApi.backlog` sizes the
  accept queue and `tapApi.limitConcurrency` makes uvicorn answer 503 past
  a per-worker connection ceiling — off by default until the reset onset is
  established, which is package 13.

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
## Package 13 — Honest overload shedding

The fixed-scaling family (run `20260824T014320Z-a5058118-fixed-scaling`)
found that wherever the connection pool tips over, a large fraction of the
shed load is dropped at the socket rather than refused with an answer:

| measurement | requests | 503 | ReadError | ReadTimeout |
| --- | --- | --- | --- | --- |
| 1 replica, 1×C1 | 4,194 | 1,330 | 893 | 586 |
| 2 replicas, 2×C1 | 15,211 | 2,594 | 11,968 | 540 |
| 4 replicas, 6×C1 | 12,421 | 7,294 | 4,012 | 753 |

The 503s are the pool-timeout path working as designed; the `ReadError`s are
not — a reset is indistinguishable from a crash to the client that receives
it, and cannot be retried safely. First hypothesis is the listen socket's
accept queue overflowing before the application sees the connection. The
knobs exist (`tapApi.backlog`, `tapApi.limitConcurrency` — uvicorn answers
503 past a per-worker connection ceiling); what is missing is the onset:
every measurement showing resets abandoned most of its arrivals at the
generator's in-flight cap, so the offered rate at which resets begin is
unknown, and the ceiling cannot be placed.

Work: a bounded-concurrency load shape in the harness (the open-loop
generator cannot hold a sustained-overload point); a run held just past pool
saturation to establish where resets start; then a default or documented
`limitConcurrency` placed above a worker's normal concurrent load and below
the reset onset.

**Resolution is shown by** a bounded-concurrency fixed-scaling run held just
past pool saturation, on one and on two replicas: the reset onset rate is
recorded, and with `limitConcurrency` set the same held load sheds with
`ReadError` at ~0 while 503s carry the refusals.

## Package 14 — Measure replica scaling

Run `20260824T014320Z-a5058118-fixed-scaling`: seventeen of 24 measurements
were generator-capped, and the seven valid points do not bracket a ceiling
at any replica count — one replica cannot serve C1 (it sheds 98.7% of it),
and two, four and eight replicas all serve 1×C1 with headroom to spare. The
suite nearly published "replica scaling efficiency at 8: 0.25" from two
rates the service met in full; efficiency figures now require a bracketed
capacity, so the column reads "—" until this package runs.

Work, benchmark-side: rungs between 1× and 2×C1 — where the interesting
region for two replicas and up turned out to be — or bounded-concurrency
runs per replica count, the shape the size sweep already uses.

**Resolution is shown by** a fixed-scaling run in which every replica count
has a bracketed capacity (a valid rung the service was pushed past), the
per-replica efficiency column populates from those brackets, and the p95 at
each count's capacity is reported alongside it.

## Package 15 — Analysis artefacts the classifier still carries

Three recorded misreadings in the bottleneck analysis, none of which changes
what was measured but all of which change what a reader concludes:

- `peak_pool_wait_p95_s` reads ~9.7 s against a 5 s pool timeout, because
  the pool-wait histogram's last finite bucket is 10 s and every timed-out
  acquire interpolates to the middle of (5, 10]. Needs a bucket boundary at
  the timeout — a service-image change, now unblocked since the image moved
  anyway.
- `CONNECTION_POOL_BOUND` takes confidence `min(1.0, pool_wait)`, so any
  wait over a second is full confidence and the class outranks everything
  else wherever the pool waited at all. Needs grading against the timeout.
- The executor-CPU ceiling is the *peak* ready replica count times one core
  across the whole window. An autoscaled fleet ramps, so mid-ramp the
  ceiling is overstated and a pinned fleet classifies `UNKNOWN` — run
  `20260824T140134Z-bf7e4b24-keda` shows exactly this on its maxed-out
  scenarios. The ceiling has to be time-aligned to the ready count.

**Resolution is shown by** `tapbench reclassify` over the depth-signal
verification run: the maxed-out scenarios stop reading `UNKNOWN` where the
per-timestamp fleet was pinned, pool-wait p95 never exceeds the configured
timeout, and `CONNECTION_POOL_BOUND` confidence grades rather than
saturates.

## Package 16 — Ship the planner what it needs

Two database-side findings, both currently documentation rather than
deployment:

- Cone search needs the index on the *expression* the translator emits —
  `spoint(RADIANS(s_ra), RADIANS(s_dec)) @ scircle(...)` — or every cone
  search is a sequential scan; a GiST index on a stored `spoint` column is
  never considered. All three functions are `IMMUTABLE`, so the expression
  index is legal, and the planner uses it once present.
- Ten plan nodes costing 5 ms or more carry cardinality estimates 50x to
  477x out, all in the join-heavy classes (Q09, Q11, Q14). Extended
  statistics on the CAOM parent/child key pairs is the obvious candidate.

Work: create the expression index and the extended statistics in the
metadata domains' bootstrap (the same ensure-path the schema uses), so a
deployment gets them without reading a guidance page.

**Resolution is shown by** the stress-class family on D2 or larger: the
cone-search classes plan through the expression index (no sequential scan
of ObsCore in their `EXPLAIN`), Q09/Q11/Q14 cardinality estimates come
within an order of magnitude, and their p95 does not regress.

## Package 17 — Finish the size sweep

D3 (25 GiB) is the first size that touches disk in earnest — buffer hit
ratio 68–70% against 100% on D1/D2 — and D4 (45 GiB) is unmeasured, so the
suite has said nothing yet about the I/O-bound regime under concurrency.
Two harness gaps block reading it well: the 60-second warmup demonstrably
does not warm a working set this size (the first repetition at each size is
colder than the rest), and every cross-size throughput comparison taken
before the per-tier corpus fix compares different workloads and was
retracted as a size effect.

Work, benchmark-side: widen the warmup for the larger tiers, run the
concurrency sweep on D3 and D4, and re-publish throughput-versus-size from
post-fix runs only.

**Resolution is shown by** a db-scaling run covering D1–D4 with one corpus,
in which the first repetition at each size is statistically indistinct from
the rest, and a published throughput-versus-size curve whose every point
comes from that run.
