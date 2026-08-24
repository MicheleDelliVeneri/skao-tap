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

### 2026-08-24 — overload now sheds with answers, and the ceiling is placed (package 13, delivered)

The bounded-concurrency shape the package asked for exists (`tapbench
shedding`, `make benchmark-shedding`): a closed loop *is* bounded
concurrency, so holding it far past saturation is the sustained-overload
point the open-loop generator could never keep still. Run
`20260824T210726Z-9b2bfb85`, D1, normal mix, held for 45 s per rung:

| held connections | 1 replica, uncapped | 1 replica, limit 64 | 2 replicas, uncapped | 2 replicas, limit 64 |
| --- | --- | --- | --- | --- |
| 32 | all served, 93 rps | all served, 92 rps | all served, 181 rps | all served, 179 rps |
| 128 | 389 × 503, 0 resets | 241 × 503, 0 resets | 223 × 503, 0 resets | 210 × 503, 0 resets |
| 512 | 502 × 503, **36 resets** | 501 × 503, **32 resets** | 511 × 503, 0 resets | 5,358 × 503, 0 resets, 49 rps |
| 2,048 | collapse: 0 × 503, 69 resets, 2,026 timeouts | same collapse | 64 resets, 2,031 timeouts | **7,876 × 503, 0 resets, 74 rps** |

Three facts worth keeping:

- **The onset is placed.** An uncapped worker starts resetting connections
  between 128 and 512 held (2.4% of requests at 512), and collapses
  entirely around 2,048 — the accept backlog — where *nothing* is answered:
  no 503s, just client timeouts at the 120 s mark. The earlier
  fixed-scaling resets were this, reached through the open loop's in-flight
  growth.
- **The ceiling works — through the fleet, not the single worker.**
  `tapApi.limitConcurrency: 64` is now the chart default: above a worker's
  concurrent load at capacity (a replica saturates at 4–8 clients), below
  the onset. Two capped replicas held 2,048 connections with every refusal
  a 503, zero resets, and 4.5× the uncapped run's goodput — refusing early
  protects the pool for the requests that are admitted.
- **Past ~512 connections per worker, no application ceiling helps.** The
  single-replica collapse is identical with and without the cap: the event
  loop drowns in accepted sockets before `limit_concurrency` can answer
  anything. That regime belongs to whatever bounds connections upstream —
  replicas behind a Service, or an ingress — and the values file now says
  so instead of implying a knob would save it.

The verification pass the package defined — the same held load, refused
with 503s instead of dropped — is the two-replica column, and it holds at
both overload rungs.

### 2026-08-24 — the aggregate was paying for the cursor, not the aggregate (package 11, delivered)

Q13 (`GROUP BY` over the whole ObsCore table) was never the price of the
scan. Result queries ran on named (`DECLARE`'d) cursors, and a cursor decides
the plan twice over: PostgreSQL never parallelises a cursor's query, and
`cursor_tuple_fraction` biases the planner toward fast-start plans on the
assumption the client stops reading early — always false here, since the
service reads every result to MAXREC + 1. On D1 the cursor ran Q13 as a
serial full **index** scan at 614 ms where the same statement, planned
plainly, runs a parallel seq scan in 195 ms with PostgreSQL's default two
workers (108 ms with four). The bias is also unstable: an earlier record
shows the cursor picking a 400 ms serial seq scan for the identical query —
two different crippled plans on two hosts, neither of them the plain one.

Result queries now run as plain streamed statements (`Cursor.stream()`,
chunked delivery, TCP backpressure against a slow reader — memory stays
flat). Stress family before (`20260824T200849Z-80cf325b`, main image) and
after (`20260824T201619Z-a5ee5628`), same corpus, D1, four clients, CSV:

| | before | after | |
| --- | --- | --- | --- |
| Q13, full aggregate | 6.7 rps, p95 655 ms | **21.7 rps, p95 269 ms** | now honestly `DATABASE_CPU_BOUND` |
| Q11, 10,000 rows | 6.2 rps, p95 745 ms | 6.8 rps, p95 681 ms | |
| Q09, deep join | 30.9 rps, p95 161 ms | 32.3 rps, p95 151 ms | |
| Q14, expensive join | 25.0 rps, p95 416 ms | 26.3 rps, p95 409 ms | |

The package's three questions, answered:

- **Parallel settings.** Q13 execution on the 8-CPU pod, best of five:
  serial 373 ms, 2 workers (the PostgreSQL default) 144 ms, 4 workers
  95 ms, 6 and 8 flat at ~92 ms — the scan floor. The default already
  captures most of it; `max_parallel_workers_per_gather: 4` is a latency
  knob for deployments where single-query aggregate latency matters, and
  `max_parallel_workers` (default 8) is what keeps concurrent aggregates
  from taking the whole pod. The chart's defaults are unchanged: the knob
  is documented in `values.yaml`, not pre-turned.
- **`EXPLAIN`-based sync rejection.** Not built, and not on these numbers:
  a 269 ms p95 needs no steering, and `syncTimeoutSeconds` now honestly
  bounds the *whole* statement (it used to bound each cursor FETCH), which
  refuses a query by what it actually costs rather than by what an
  estimate guessed. The open case is D3, where this query held a sync
  connection for 18 s pre-fix — remeasured post-fix under package 17
  before deciding.
- **Summary tables / materialised views.** A data-domain decision, not
  service infrastructure: the service's job was to stop charging 6× the
  scan's honest price, and nothing at D1 justifies maintaining
  precomputed answers. Revisit only if the D3/D4 numbers say the honest
  price is still too high for the queries users actually run.

Two things travelled with the fix. The abort path used to recognise a job's
backend by the cursor's *name* in `pg_stat_activity`; that identity now
rides as a leading SQL comment (`uws.job_query_tag`), placed first so
statement truncation can never hide it — the component suite's abort tests
dropped from 87 s to 30 s because cancels now land instantly. And the plans
the suite records with `EXPLAIN` (`plan-flags`) were always plain-statement
plans; the service now executes what the suite was measuring.

### 2026-08-24 — the large-result cost was the per-cell type question (package 10, delivered)

Package 10 asked where the `SERIALIZATION_BOUND` time went. Into asking,
110,000 times per Q11 response, what type each cell was — a question the
column's PostgreSQL type OID had already answered before the first row
arrived. `_plain()` ran once per cell in every format, up to four
`isinstance` checks deep; VOTable ran a second per-cell function on top and
then `saxutils.escape`, three unconditional `str.replace` allocations on
text (ObsCore identifiers, URLs) that contains none of the three characters
it looks for. The fix types the dispatch per *column*: a kind now names the
Python type psycopg produces as well as the wire type, recognised values
reach the writers untouched, and only `numeric`, timestamps and the
`opaque` residual carry a per-cell coercion.

Measured by the new `result-formats` family — runs
`20260824T172808Z-00d532f7` (before) and `20260824T181653Z-a5e3d315`
(after), same D1 (2.16 GiB, 700k ObsCore rows), same corpus hash, same
five Q11 query texts in the same order, 4 closed-loop clients, 3
repetitions, zero errors and zero invalid measurements in either run. Q11,
10,000 wide rows per response:

| Q11 | rps | p95 | bytes/response |
| --- | --- | --- | --- |
| votable | 4.2 → **7.6** (1.82×) | 1,109 → **615 ms** | 3.46 MiB |
| json | 4.8 → **8.7** (1.83×) | 979 → **544 ms** | 2.74 MiB |
| csv | 4.4 → **6.2** (1.42×) | 1,060 → **758 ms** | 2.52 MiB |
| tsv | 4.4 → **6.2** (1.42×) | 1,051 → **748 ms** | 2.52 MiB |
| parquet | 8.4 → **17.9** (2.12×) | 577 → **307 ms** | 0.54 MiB |
| arrow | 8.3 → **17.4** (2.09×) | 574 → **313 ms** | 2.16 MiB |

Q10 (1,000 rows) moved the same direction, 1.28–1.51× across the six.
In-process (`tapbench serialize`, no cluster in the way) the writers
themselves went arrow 8.01 → 0.93 µs/row, parquet 8.77 → 1.78, json
15.44 → 5.12, votable 17.94 → 7.04, csv 17.05 → 9.19. The text output is
byte-for-byte what it was — a differential test over every kind, its
boundary values and NULL holds it there, and the measured bytes/response
agree to ±0.03%.

Qualifications: these are warm-cache numbers by design — D1 fits inside
PostgreSQL's 12 GiB entirely, and the family holds the database cost
constant to isolate the writer; and the Q11 corpus is five distinct query
texts (its template uses only the collection parameter), identical across
formats and builds, so the comparison is clean but the absolute p95 is not
a cold-archive figure. Two of three after-run parquet repetitions now
classify `TAP_CPU_BOUND` rather than `SERIALIZATION_BOUND` — for the
columnar formats the writer has stopped being the dominant cost. What
remains in CSV, still the slowest of the six, is `_csv.writer`'s own
quoting logic, which is CPython's floor rather than the service's — the
recommendation for bulk transfer is Parquet (or Arrow on a fast link), now
documented in [Result formats](result-formats.md). First run on the new
30-core/120 GiB host and its budget (24-core node, 8-CPU/12 GiB
PostgreSQL): absolute numbers do not compare to the old host's family.

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
such request occupies a connection for eighteen seconds. These numbers were
taken when result queries still ran on cursors (serial, fast-start-biased
plans; see the package 11 entry above): D3 must be remeasured with parallel
plans before the admission decision is made, which package 17's sweep now
carries. What already stands either way is that `syncTimeoutSeconds` bounds
the whole statement, so a runaway synchronous aggregate is refused by its
actual cost rather than holding the connection indefinitely.

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
post-fix runs only. Include the stress classes: Q13 held a sync connection
for 18 s on D3 when it ran on a cursor (serial, fast-start-biased plan), and
whether a parallel plan brings that inside a defensible `syncTimeoutSeconds`
is the measurement the deferred `EXPLAIN`-based admission decision (package
11) is waiting on.

**Resolution is shown by** a db-scaling run covering D1–D4 with one corpus,
in which the first repetition at each size is statistically indistinct from
the rest, and a published throughput-versus-size curve whose every point
comes from that run.
