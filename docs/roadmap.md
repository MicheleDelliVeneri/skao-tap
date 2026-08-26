# Roadmap

Follow-up work is organized in numbered packages, referenced by number in
issues, PRs and discussions. Package numbers are stable: delivered packages
are removed from this page but their numbers are not reused.

**Packages 18, 19 and 21 are open.** Everything through package 17 is
delivered and merged, and package 20 is delivered with this change; what
each one settled is recorded in the findings below.

## Measured findings

A running log of what the benchmark suite
(`benchmarks/egernia-performance`, see [Benchmarking](benchmarking.md)) has
established, newest first. Each entry is a measurement rather than an opinion,
so it can be checked and it can go stale — the run that produced it is named.

Like delivered packages, findings whose fixes have shipped and been verified
are removed from this page; git history keeps them, and the runs that
produced them remain in `benchmarks/egernia-performance/results/`.

### 2026-08-26 — the parse removal, deployed: executors stopped being the async bottleneck (package 20, delivered)

The autoscaling family (K1–K7), run twice on identical offered load — the
ladder anchored at C1 = 6.83 jobs/s in both — differing only in whether the
executor re-parses each job's query: run `20260826T013544Z-88d8c5e9` with
the parse in the loop, run `20260826T050451Z-0f62a0fa` with the table list
carried on the job row (`query_tables`, package 20's fix; the local profile
behind it — 190 ms of per-job re-parse against ~6 ms for everything else,
by `tests/performance/profile_async_job.py` — is in the 2026-08-25 entry in
git history). Every validity guard passed in both runs. Completions are
counted server-side: a `uws.jobs` row is inserted once and updated exactly
four times on its way to COMPLETED, so per-window completions are the
table's `n_tup_upd / 4` — the client's own success count is a casualty in
both runs, for two different reasons below.

| | before (`88d8c5e9`) | after (`0f62a0fa`) |
| --- | --- | --- |
| executor CPU per completed job | 385–469 ms | **4.3–5.0 ms** |
| per-executor drain, fleet saturated | 2.3–2.5 jobs/s at ~0.9 core/pod (K3, K6) | never saturated — ≥6.2 jobs/s at 2–3% CPU (K6) |
| sustained 6×C1 — K6, ~41 jobs/s offered | 80% done in-window; queue peaks 20,870; oldest job 851 s | 100% done in-window; queue peaks 52; oldest job 1 s |
| KEDA replicas | pegged at 8 | peak 5, mean 3.7 |

The before run is the published ~2 jobs/s figure re-measured and confirmed:
a pegged executor drains 2.3–2.5 jobs/s at ~0.9 cores — ~0.4 s of CPU per
job — and a fleet of eight cannot hold 6×C1: the queue grows past twenty
thousand, jobs wait fourteen minutes, and 94% of K6's clients gave up
before their job ran. The after run is the local profile transferring to
the deployed environment almost exactly: ~4.6 ms of executor CPU per job
against the profile's ~6 ms, ninety times less than before. No scenario in
the family can make the executor the bottleneck any more — every job every
scenario offered completed inside that scenario's window, KEDA never ran
more than five replicas, and the pods that did run sat 97% idle.

What the after run surfaces instead is the async path's next ceiling: the
API. From 3.5×C1 up, 17–57% of client conversations failed — with
connection errors, on jobs the server had accepted and did complete —
against the single API pod at ~85% of its one-core limit. A job is a
submit plus phase polls plus a result fetch, and ~41 arrivals/s of that
control traffic exceeds what one pod that also answers ~99 rps of sync
queries can serve. The executor autoscaling defaults survive unchanged
(`queuedJobsPerReplica: 10` — the queue simply never builds); the ~2
jobs/s notes in `values.yaml`, `autoscaling.md` and `architecture.md` are
republished from these runs. What one executor can do now has no measured
ceiling: feeding it one would take an API that scales, which is not this
package.

One boundary before that ceiling is read as a service property. The
executor numbers above are server-side and client-independent; the API
ceiling is a joint property of the service and a spin-polling client. The
generator never sends UWS 1.1 `WAIT`: it polls each job's phase at 0.25 s
backing off to 2 s, so one job costs the API roughly six to eight
requests, and package 18 put this API at 10.5 ms of CPU per request
(96–100 rps at one worker) — ~100 rps over ~7 requests per job is ~14
jobs/s, consistent with where the failures set in (clean at 0.5×C1 ≈ 3.4
jobs/s, failing at every rung from 3.5×C1 ≈ 24 jobs/s up; the family has
no rung between). The service already carries the lever the generator did
not use: `uws_api.py` implements the blocking `WAIT`, which would replace
most of those requests with one held connection per job — so an operator
hitting this ceiling may want a client change before more API replicas.
Measuring the async ceiling under a `WAIT`-ing client was not exercised
here and is a clean small follow-up.

### 2026-08-25 — the size sweep is finished, and size almost is not the story (packages 16 and 17, delivered)

One db-scaling run (`20260825T005436Z-b450b0a9`), one corpus, D1 through
D4 grown in place, every measurement guard-valid and none invalid. The
throughput-versus-size curve is republished from this run alone
([results](performance/index.md)), and it is nearly flat:

| | at saturation (c=4) | single client | classified |
| --- | --- | --- | --- |
| D1, 2 GiB | 98.0 rps, p95 63 ms | 85.0 rps | `TAP_CPU_BOUND` |
| D2, 10 GiB | 94.1 rps, p95 66 ms | 79.9 rps | `TAP_CPU_BOUND` |
| D3, 25 GiB | 92.4 rps, p95 67 ms | 78.0 rps | `TAP_CPU_BOUND` |
| D4, 45 GiB | 91.9 rps, p95 67 ms | 75.9 rps | `TAP_CPU_BOUND` |

Twenty-two times the data costs six percent of the throughput. The tier
that was "where the working set stops fitting" — D3, 68–70% hit ratio, up
to 52 s of read wait per window — became `TAP_CPU_BOUND` the moment the
cone-search expression index existed (package 16): the cone classes had
been sequentially scanning the whole ObsCore table, and that, not the
mix, was the I/O regime. What remains of size is visible only at a single
client (D4 reads `DATABASE_IO_BOUND` there, 11% under D1) and in the one
class whose work is proportional by construction.

- **The warmup fix holds.** Each tier's declared warmup (300 s at D3,
  600 s at D4) makes the first repetition statistically indistinct from
  the rest — within 1% at every size — where it used to be the coldest
  point in the set.
- **Package 16's plan verification.** No cone-search class sequentially
  scans ObsCore at any size, at last: `Q05/Q06/Q07/Q12` all plan through
  `obscore_spoint_gist`, the index the schema had described in a comment
  and asserted in a test expectation without ever creating. The remaining
  `bad_cardinality_estimate` flags concentrate where they should: Q14's
  unanchored `LIKE` is unestimable by design, and a share of the recorded
  ratios on Q10/Q14 are the LIMIT artefact (a node above a `TOP` stops
  early, so "actual" undercounts and the ratio inflates) — a flag-quality
  observation, not a planner failure.
- **The deferred admission decision: no `EXPLAIN` gate.** Q13, the
  full-table aggregate, is the one class that scales with size — p95
  292 ms / 1.23 s / 3.04 s / 6.67 s across D1–D4, `DATABASE_CPU_BOUND`
  with parallel plans throughout (it was 17.8 s and I/O-bound on D3
  before the cursor fix). Even at D4 that sits inside a sane
  `syncTimeoutSeconds`, and under four concurrent D3 aggregates the pool
  shed 23% with 503s — which is the admission mechanism working: bound by
  actual cost, refuse with an answer when concurrent cost exceeds the
  pool. A cost-estimate gate would add a planning round trip to every
  query to refuse queries a timeout already bounds. If a deployment's
  users hammer full aggregates, the fix is a summary table in the data
  domain, not a smarter gatekeeper.

### 2026-08-25 — replica scaling measured: 0.73 at eight, and it is a ceiling (package 14, delivered)

Run `20260824T235130Z-ccbcb41a`, the closed-loop sweep once per replica
count, same ladder and workload seeds at every count, every capacity a
saturated sweep's plateau rather than the largest rate anybody offered:

| replicas | capacity | scaling |
| --- | --- | --- |
| 1 | 98.9 rps (C1) | 1.00 |
| 2 | 190.8 rps | 0.96 |
| 4 | 347.5 rps | 0.88 |
| 8 | 577.9 rps | **0.73** |

The number that did not survive: the first run of this family published
an eight-replica ceiling of 403 rps (efficiency 0.51) that was the load
generator's own — one asyncio event loop pinned at 100% of a core, read
as "3% of the host" because the self-watch divided by the core count, so
the guard built to catch exactly this stayed green. With the peak judged
against one core and the family's held concurrency sharded over four
processes, the busiest generator process peaked at 47% and the plateau is
the service's (`TAP_CPU_BOUND` at the top rung). The 27% lost by eight
replicas is the shared substrate — one PostgreSQL, one node — and is now
a number with a measured failure on each side of it.

### 2026-08-24 — the classifier's four misreadings, corrected (package 15, delivered)

None changed what was measured; all changed what a reader concludes, and
each is now pinned by a test that fails if it comes back:

- **Pool waits above the timeout were an interpolation artefact.**
  Reclassifying the shedding run (`20260824T210726Z-9b2bfb85`) shows the
  shape exactly: every saturated rung reports `peak_pool_wait_p95_s` of
  9.7 s against a 5 s timeout — the midpoint of the (5, 10] bucket, where
  every timed-out acquire landed because no bucket edge sat at the
  timeout. The service's histogram now derives its edges from
  `dbPoolTimeoutSeconds` (an edge at the timeout and one 20% past it), so
  runs on the fixed image read within 20% of the truth; the verdict's
  evidence now carries `pool_timeout_s` so the artefact is at least
  legible in old runs.
- **`CONNECTION_POOL_BOUND` confidence grades against the timeout.**
  `min(1.0, wait)` made any wait over a second a certainty that outranked
  every other verdict; a 0.5 s wait against a 5 s timeout is now a tenth
  of a case.
- **CPU ceilings are time-aligned to the fleet that was ready.** The
  executor (and API) ceiling was the *peak* ready count times the per-pod
  limit across the whole window, so a ramping fleet was judged mid-ramp
  against pods that did not exist yet, and a fleet pinned at 2, then 8
  cores — pinned the entire time — read `UNKNOWN` against an 8-core
  ceiling it only reached at the end. Each CPU sample is now judged
  against the replica count ready at that sample's timestamp (stepwise,
  from the run's own series). The KEDA run the package named is not in
  this checkout's results, so the correction is pinned by unit tests on
  exactly that ramp shape rather than by a reclassify.
- **The alignment covers every gate that compares against a fleet
  ceiling** — both `hot` gates and the `SERIALIZATION_BOUND` gate, the
  most sensitive of the three because its bar is 60% of the ceiling
  rather than 90%: a ramping fleet formatting bytes the whole window read
  as a fifth busy against the peak-fleet ceiling, missed the 0.25
  threshold, and was filed as something else. And `_timed_series()` now
  insists on timestamps rather than quietly judging a window on whichever
  subset of rows carried one — every row a measurement produces has one,
  and a path that only *mostly* had them would have computed a plausible
  number over a fraction of the window.

### 2026-08-24 — overload now sheds with answers, and the ceiling is placed (package 13, delivered)

The bounded-concurrency shape the package asked for exists (`egernia_bench
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
In-process (`egernia_bench serialize`, no cluster in the way) the writers
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
analysis build; `egernia_bench reclassify` re-derived them from the stored
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

## Package 18 — Name the API's per-request CPU

Every scaling recommendation on this page rests on `TAP_CPU_BOUND`, and the
only cause it ever named is gone. ADQL translation was 41 ms of a ~50 ms
request when the API's ceiling was first attributed; the fast path took it to
1.2 ms (35x, `e38ed30`), and nothing has re-attributed what remains. One
uvicorn worker is one GIL-bound thread, so the newest single-replica runs put
the budget at roughly 10 ms of CPU per request — 98.0 rps at saturation on D1
(`20260825T005436Z-b450b0a9`) and 98.9 rps at C1
(`20260824T235130Z-ccbcb41a`), both at `tapApi.replicas: 1`,
`tapApi.workers: 1` — of which translation is now 1.2 ms. About 88% of the
ceiling is unaccounted for, and two published figures already disagree about
it: the 2026-08-23 finding above says a worker is worth "roughly 200
requests/s", where these runs measure half that.

The per-request profile is also missing the one cost production always pays.
No benchmark enables authentication — `benchmarks/egernia-performance/config/chart-values.yaml`
configures no OIDC issuer — so every capacity figure on this page is an
unauthenticated figure, while a deployment that gates its endpoints verifies a
token on exactly this CPU-bound path.

Work: profile a saturated `tap-api` worker under the closed-loop normal mix
(`py-spy` against the worker process, the tool
[Python performance](python-performance.md) already prescribes for the
micro-benchmarks) and attribute the 10 ms to named frames — request parsing
and parameter validation, psycopg row conversion, the result writers at small
row counts, observability instrumentation, and the translation that is now a
twelfth of it. `tests/benchmarks/test_hot_paths.py` measures translation and
two serializers but never a whole request, so a regression anywhere else is
currently invisible; whatever the profile names belongs there as a hot path.

**Resolution is shown by** a profile of the saturated worker in which named
frames account for at least 80% of a request's CPU, published as a finding
here; a hot-path benchmark covering the request path end to end whose
per-request total agrees with the measured saturation throughput within 20%;
and one measured rung with authentication enabled, so the cost of verifying a
token on this path is a number rather than an assumption.

## Package 19 — Workers against replicas, on one host

The replica curve (1.00 / 0.96 / 0.88 / 0.73 at eight,
`20260824T235130Z-ccbcb41a`) was measured at one worker per pod against a pod
whose CPU limit is 2, so every point in it was half-idle by construction — and
the chart still defaults `tapApi.workers: 1`. Two pages carry an explicit note
that the worker figures predate the translation fast path and are being
re-measured ([Autoscaling](autoscaling.md), [Deployment](deployment.md)); they
have not been. Until they are, an operator has a measured answer for the axis
that costs pods and a stale one for the axis that costs nothing.

Work, benchmark-side: sweep `tapApi.workers` (1, 2, 4) within one pod at its
CPU limit against the existing replica ladder, same host, same corpus, same
workload seeds — and state the connection arithmetic each choice implies,
since a pod's pool ceiling is `workers x dbPoolMax` and the two axes are
therefore not interchangeable at the database.

**Resolution is shown by** a capacity figure per (workers, replicas) point,
each bracketed by a saturated sweep the way the replica ladder now is; the
stale notes in `autoscaling.md` and `deployment.md` replaced by those numbers;
and a stated default for `tapApi.workers` that follows the pod's CPU limit if
the measurement supports one.

## Package 21 — ADQL 2.1

`/capabilities` declares ADQL 2.0 only, which is what the service implements:
the parser is `queryparser`'s ANTLR grammar, and that grammar predates 2.1.
The gap is not theoretical. The geometry-column argument form the ObsCore
footprint queries need — `INTERSECTS(s_region_geom, ...)`, a column where 2.0
allows only a constructor — is a syntax error to that grammar, and `adql.py`
reaches it by hiding the column behind a sentinel `POLYGON` literal with magic
coordinates and swapping the emitted pgsphere literal back afterwards, pinned
by unit tests so an upstream change fails loudly rather than quietly producing
wrong SQL. What 2.1 adds beyond that is what current clients write: `ILIKE`,
`OFFSET`, `CAST`, bitwise operators, `IN_UNIT`.

This is the largest user-visible gap left now that the performance surface has
been worked over, and it is also the project's narrowest dependency: the whole
query layer rests on one ANTLR grammar from a single upstream, wrapped in a
substitution the service has to keep honest.

Work: settle the parser question before writing any grammar — fork
queryparser's grammar to 2.1, or replace the parse with a maintained
alternative — and only then the feature surface, the sentinel's removal, and
the declared version. Note that the parse is also the subject of package 18's
profile, so the two packages share evidence: a replacement parser has to hold
the fast path's 1.2 ms as well as accept more syntax.

**Resolution is shown by** `/capabilities` declaring 2.1 truthfully; the
sentinel substitution deleted because the grammar accepts a geometry column
directly; a conformance test per added construct; and no regression in the
translation hot-path benchmark.
