# TAP / PostgreSQL / KEDA benchmark suite

Measures five things separately, because they fail separately: the TAP service
itself, PostgreSQL as the data grows, horizontal replica scaling, the
behaviour of the autoscaler, and what a result format costs to produce.

```bash
make benchmark-smoke            # ~10 min: everything wired, nothing measured for long
make benchmark-db-scaling       # concurrency sweep at each dataset size
make benchmark-fixed-scaling    # replica scaling, autoscalers off
make benchmark-keda             # autoscaling scenarios K1-K7
make benchmark-result-formats   # every result writer over the same rows
make benchmark-stress           # just the stress classes, each on its own
make benchmark-serialize        # the writers alone, in process, seconds, no cluster
make benchmark-full             # every family, every dataset
make benchmark-report RUN=<dir> # redraw plots and HTML for an existing run
```

Every target takes `RESUME=<results-dir>` and continues where it stopped, and
`NO_BUILD=1` to skip rebuilding images. Results never overwrite: a run
directory is named for its start time and commit, and the suite refuses to
write into one that exists.

## What it guarantees

**Deterministic.** The data is generated server-side from a hash of (seed, row
index, field), so a batch re-run writes identical rows and generation is
resumable. The query corpus is built from the same hash — which is why a cone
search can be aimed at a position where objects actually are without querying
the database — and hashed into the provenance of every result. Arrival times in
the open-loop generator come from a fixed seed, not from `hash()`, which is
randomised per process.

**Resumable.** Each measurement writes its own Parquet and a completion
marker. A 40-hour matrix that dies at hour nine resumes at hour nine.

**Honest about validity.** A run is *marked*, never silently accepted, when the
host swapped, a container was OOM-killed or restarted, disk headroom fell below
the floor, Prometheus lost samples, or the load generator itself went above 80%
CPU. The samples are kept either way: they are the evidence of what went wrong.

## Layout

```
config/         hardware budget, dataset targets, scenarios, chart values
manifests/      kind cluster, Prometheus + kube-state-metrics, NodePort
egernia_bench/
  cluster.py    cluster lifecycle and the resource caps
  corpus.py     Q01-Q14 and the deterministic parameter corpus
  runs.py       run directories, provenance, resumability
  dataset/      schema, server-side generator, size-driven growth
  load/         closed-loop and open-loop generation, per-request samples
  collect/      Prometheus, PostgreSQL statistics, Kubernetes, guards,
                py-spy profiles, the OIDC stub issuer
  analyze/      statistics, KEDA timings, bottleneck rules, plots, HTML
  orchestrate/  what runs in what order
results/        one directory per run (git-ignored)
```

## What it needs installed

Docker, `kind`, `kubectl` and **Helm 4**. Helm 3 fails at chart install with
`unknown flag: --force-conflicts`: the chart is applied server-side, and the
KEDA family changes replica counts through the scale subresource, so the next
upgrade has to be able to take those fields back.

Public images (KEDA's three, Prometheus, kube-state-metrics) are pulled on the
host and imported into the node, not pulled by the node — a host that reaches
a registry through a proxy or an image mirror can then still bring the cluster
up. `docker save --platform linux/amd64` rather than `kind load
docker-image`: with Docker's containerd image store a multi-platform reference
saves manifests whose blobs are not local, and the node's importer rejects the
archive for a digest it cannot find.

## The hardware budget, and how it is imposed

`config/hardware.yaml` holds both the intended budget and how it is enforced.
kind has no CPU or memory setting of its own, so the cap goes on the node
*container* (`docker update --cpus 24 --memory 96g`), and the load generator
runs on the host outside that cap — otherwise it competes for the cores it is
measuring. Its own CPU is watched anyway, because a saturated generator
produces exactly the flat throughput curve a saturated service does.

## Datasets

Four sizes by **actual `pg_database_size()`**, indexes included — not row
counts multiplied by an assumed width:

| | target | how it is reached |
| --- | --- | --- |
| D1 | 2 GiB | one database, grown |
| D2 | 10 GiB | …further |
| D3 | 25 GiB | …further |
| D4 | 45 GiB | …further |

One growing database checkpointed at each target, so D4 costs 45 GiB of disk
rather than 82, and every tier is a genuine prefix of the next. Recorded per
tier: database size, per-table and per-index bytes, row counts, ObsCore rows,
index/table ratio. `VACUUM ANALYZE` runs at each checkpoint.

The schema is the service's own ODP hierarchy — projects, observations,
scheduling blocks, execution blocks, data products, artifacts — with
`ivoa.obscore` as the plugin's view over it, and the wide text
columns (`s_region`, `access_url`, `obs_publisher_did`) that decide how many
rows fit in a page — row width is half of what an I/O benchmark measures, so it
is neither padded nor trimmed.

### The spatial index is not optional

The ADQL translator emits

```sql
spoint(RADIANS(s_ra), RADIANS(s_dec)) @ scircle(...)
```

— a function applied to the columns. A GiST index on a stored `spoint` column
would never be considered; the index has to be on that expression:

```sql
CREATE INDEX ... ON ivoa.obscore USING gist (spoint(radians(s_ra), radians(s_dec)));
```

`spoint`, `scircle` and `radians` are all `IMMUTABLE`, so this is legal. The
suite checks that the planner actually uses it rather than assuming
(`expected_index_unused`).

### New schemas need grants

The service downgrades to `tap_reader` before running user SQL, and that role
is granted rights on the schemas that existed at database initialisation. A
schema added later is invisible to it and every query fails with *permission
denied for schema* — so the suite grants explicitly. This applies to any
deployment adding a metadata domain, not just to this benchmark.

## Query corpus

Fourteen classes, 12,000 distinct parameter combinations, drawn from the
generated data:

| | class | exercises |
| --- | --- | --- |
| Q01 | TAP_SCHEMA metadata | the floor: parse, plan, serialise, respond |
| Q02 | identifier lookup | btree point lookup |
| Q03 | categorical filter | composite btree, low cardinality |
| Q04 | temporal range | two-column btree range scan |
| Q05 | small cone | pg_sphere GiST on the translated expression |
| Q06 | medium cone | the same index where the heap fetch dominates |
| Q07 | spatial + time + metadata | three predicates, three indexes |
| Q08 | observation→product join | the join clients actually make |
| Q09 | four-level ODP join | join depth with fan-out (stress) |
| Q10 | ~1,000 rows | serialisation and streaming |
| Q11 | ~10,000 rows | where the response body dominates (stress, and the class the result-format family runs) |
| Q12 | empty spatial result | the cost of finding nothing |
| Q13 | aggregation | full scan and hash, tiny answer (stress) |
| Q14 | deliberately expensive | five-way join, unanchored LIKE, forced sort (stress) |

The normal mix is Q01 5%, Q02 15%, Q03 15%, Q04 10%, Q05 25%, Q06 10%,
Q07 10%, Q08 5%, Q10 5%. Q09/Q11/Q13/Q14 run on their own: one Q14 in a
hundred requests moves the p99 of everything else and tells you nothing about
either. `egernia_bench stress` runs just those four, each at fixed concurrency —
minutes instead of a whole family, for a change aimed at one expensive class.

## Load generation

**Closed loop** (N clients, each issuing the next request when the last
returns) for capacity and latency at a known parallelism. **Open loop**
(Poisson arrivals at a rate, whether or not the service keeps up) for anything
involving a queue — a closed-loop client cannot overload anything, because it
slows down exactly as much as the service does. `t_offered` is recorded
alongside `t_start`, so the generator's own lateness is measurable rather than
absorbed into the service's latency.

**Which replica answered is recorded per request**, from the `X-Served-By`
header the service sets for exactly that purpose. It matters because a
closed-loop client holds keep-alive connections, so kube-proxy assigns each
client to a pod once per *connection* rather than per request: at low
concurrency against several replicas the assignment is a coin flip that then
persists for the whole window, and two clients that land on the same pod
measure one pod. Observed as 177.6 rps against 98.1 rps on two repetitions of
identical work at two clients and two replicas. Every measurement now reports
`served_by` — the pods that answered and the busiest one's share — so a rung
where the clients collapsed says so, instead of the median of three
repetitions absorbing it. It vanishes at the saturating rungs, which is where
capacity is read, so no published capacity figure was affected.

The concurrency ladder is 1, 2, 4, 8, 16, 32, 64, 128 and stops when **two**
saturation signals agree: throughput gain under 5%, p95 over 5x baseline,
errors over 1%, API CPU over 95% of its ceiling, PostgreSQL CPU over 95%. One
signal alone is routinely noise. A saturation point is then re-measured for
300 s x 5 repetitions, because it becomes C1 and every autoscaling scenario is
expressed as a multiple of it.

The API's CPU ceiling is `min(pod CPU limit, workers)`, not the pod limit: ADQL
translation holds the GIL, so one worker cannot exceed one core whatever the
cgroup allows. Comparing against the pod limit reports a pinned process as
having headroom.

## Result formats

The generator asks for CSV unless told otherwise, and for a long time nothing
told it otherwise — so every latency the suite ever published was a CSV
latency and nothing said so. The format is now recorded on every measurement
and varied deliberately by one family:

```
egernia_bench result-formats     Q10 and Q11 through all six writers, fixed concurrency
egernia_bench serialize          the writers in process: no cluster, no database, no HTTP
```

They answer different questions and neither answers the other's. Behind an
HTTP request the writer is a tenth of the response time and the database is
most of the rest, so `result-formats` says what a *client* pays for asking for
a format — bytes on the wire included — while `serialize` says what the
*writer* costs, which is the number a change to the serialisation path moves.
`serialize` runs in seconds and needs nothing installed, so it is the one to
run while making such a change; `result-formats` is what confirms the change
reached a user.

Both hold everything but the format still: the same query class, the same
corpus entries in the same order, so the rows found and fetched are identical
and the difference between two measurements is the writer.

## Per-request CPU, and what a token costs

```
egernia_bench profile            where one worker's per-request CPU goes, and a token's cost
```

Every scaling recommendation this suite produces rests on `TAP_CPU_BOUND`, and
for a long time the only cause that classification named was ADQL translation
— 41 ms of a ~50 ms request when the ceiling was first attributed. The fast
path took translation to 1.2 ms and nothing re-attributed what remained, so
"the API is CPU-bound" became a claim with 1.2 ms of evidence behind roughly
10 ms of cost.

This family answers it, at `replicas: 1, workers: 1` throughout so that "per
request" and "per worker" are the same statement. Six rungs at one
concurrency, chosen by the same short ladder that finds every published
single-replica figure:

| rung | what it is |
| --- | --- |
| `base` | unauthenticated, unprofiled — the reference |
| `gil` | the same load, py-spy holding to GIL-owning stacks |
| `all` | the same load again, every non-idle thread sampled |
| `authverify` | every request carries a token the service verifies; nothing gated |
| `authgil` | the authenticated rung, profiled |
| `authgated` | the same, with the whole query surface enforced |
| `noauth` | authentication off again |

**Two profiler passes, because a worker has two exhaustible resources.**
`--gil` samples only stacks holding the interpreter lock, which is the resource
a single worker's throughput ceiling is *made of*. Sampling every non-idle
thread adds the work done with the lock released — libpq on the socket, the
writers' C extensions, the threadpool handoff — which is CPU the ceiling does
not contain. The two are compared as distributions (`share_off_gil`): a
subsystem larger among on-CPU samples than among GIL-holding ones is doing
work the ceiling does not contain.

**The split comes from the samples; the total comes from the cgroup.** py-spy
says where the time goes, not how much of it there is. The denominator is
Prometheus' CPU accounting for the same window over the requests that window
served. Every per-subsystem millisecond figure is the product of the two, and
neither alone.

This is not fastidiousness. In nonblocking mode py-spy reached ~41 Hz of the
100 it was asked for, so a pass's sample count is *not* a duration: read as
interpreter-lock occupancy per request it would be out by a factor of two and a
half. `profiled_occupancy_ms_per_request` is
therefore `None` whenever the sampler missed its rate, with
`occupancy_unavailable_reason` saying so and `achieved_sample_rate_hz` beside
it. What the shares still support is the split; the total is what the cgroup is
for.

**A sample is attributed to the innermost frame that names a subsystem.** Leaf
self-time is the wrong unit on its own — half of a serialiser's cost is stdlib
`csv`, half of the translator's is `re` — so a leaf table says the hot function
is `re.match` and names nothing anybody can act on. Rolling stdlib leaves up to
the nearest named caller answers the question actually being asked: which
*part of the request* the time belongs to. The share matching no rule is
reported as a residual rather than folded into a bucket.

**Sampling is nonblocking, and that is a measurement rather than a
preference.** py-spy's default is to pause the process while it walks the
stacks. Against a saturated `tap-api` worker that cost **74% of its
throughput** — 95.5 rps unprofiled, 24.9 rps profiled at 100 Hz — and then got
the pod killed: a worker stalled that hard cannot answer `/health/live` inside
its one-second timeout, so the kubelet restarted a process that was busy rather
than broken. Dropping to 5 Hz still cost 45%, because the expense is per
attach (~100 ms of stall) and not per sample. Nonblocking cost 0.9% of throughput
and buys torn stacks instead: py-spy discards the reads it can detect as
inconsistent (kept as the profile's `error_fraction`) and misattributes the
ones it cannot. Three of 24,400 samples in the published pass arrived with a
frame name that was not even valid UTF-8, which is the visible floor of that
bias rather than its measure — those are counted too. `--blocking` takes the
accurate reading from a process that can survive it.

The cost is measured either way. The profiled window is compared against the
unprofiled rung before it, at the same concurrency on the same pod, and a
profile costing more than `max_overhead_fraction` of throughput is marked
invalid on the measurement as well as on the run — so the report leaves the
attribution out rather than publishing a breakdown of a worker its own profiler
slowed down. That guard is what produced the numbers above:
`20260825T155319Z-44a69b9c-profile` is the run where it fired.

**Authentication is real, not simulated.** `collect/oidc.py` generates a
2,048-bit RSA keypair for the run, publishes its public half as a JWKS behind
an in-cluster Service with a discovery document naming itself as the issuer,
and mints RS256 tokens on the host. The service does everything it would do
against an INDIGO IAM: discovery, `kid` lookup, signature, issuer, expiry,
audience — there is no verified-token cache in it, deliberately, so every
request pays a full verification. What is missing is only the IAM's own
latency, paid once per JWKS lifetime rather than per request. The private key
is generated in the process and discarded with it; nothing is committed.

Two failures this family refuses rather than reports:

- **A rung whose pods do not carry its policy.** Configuration reaches these
  pods through a ConfigMap, which a container reads once at startup, so a
  `helm upgrade` changing only ConfigMap data is a successful upgrade no
  running pod has read. The chart hashes the ConfigMap into both pod templates
  so such a change is a rollout; every rung then reads `/api/v1/auth` — the
  service's own statement of what it enforces — and refuses to measure if it
  disagrees. An "authenticated" rung served by pods with authentication off
  measures the unauthenticated service twice and reports the difference as
  zero.
- **A gated rung nobody is allowed to use.** The token's group is the group
  `config/auth-values.yaml` grants every gated operation to, so the
  authorisation decision succeeds. A rung where every request is denied
  measures the 403 path.

The authenticated rungs are bracketed by two unauthenticated ones (`base`
before, `noauth` after) and the comparison is against their mean: two helm
upgrades and a pod restart separate them, and drift over a run would otherwise
be indistinguishable from the cost of a token.

## Autoscaling

The scenarios drive **`/tap/async`**, not `/tap/sync`. The repository's
ScaledObject scales *executors* on the age of the oldest queued job, so only
work that creates jobs can move the scaler metric; a sync workload would leave
it at zero and measure nothing. The chart's own configuration is used as it
renders, and the exact ScaledObject and HPA YAML are saved with every run.

Nine stamps, each read from the party responsible for it rather than inferred
from one clock:

| | |
| --- | --- |
| T0 | the load changed (load timeline) |
| T1 | the scaler's metric crossed its threshold (`keda_scaler_metrics_value`) |
| T2 | the HPA changed its request (HPA status, polled every 2 s) |
| T3–T6 | Pod created / scheduled / container started / Ready (pod conditions) |
| T7 | served traffic (per-pod CPU, else first success after Ready — method recorded) |
| T8 | p95 back inside the SLO for three consecutive windows |

A stage that cannot be established is `null` with the reason recorded. A
guessed timing is a wrong answer that looks like evidence.

## Output

Per run directory:

```
summary.json summary.csv environment.json dataset.json report.html
samples/*.parquet          every request: timestamp, class, id, status,
                           latency, TTFB, bytes, offered time
metrics/*.parquet          every Prometheus series at 2 s resolution
postgres/*-before|after|delta.json, *-statements.csv
kubernetes/*-state.jsonl, *-pods.json, *-events.json,
           scaledobject.yaml, hpa.yaml
explain/*.json             EXPLAIN (ANALYZE, BUFFERS) plus flags
plots/*.png *.svg
guards.json invalid.json   validity
```

Sixteen plots plus one synchronised dashboard per autoscaling scenario. A plot
that cannot be drawn appears in the report with the reason, because a report
with fifteen of sixteen plots and no explanation is indistinguishable from one
where the sixteenth was never asked for.

## Bottleneck classification

Each class is a rule over measured quantities, reported with the numbers that
made it fire: `TAP_CPU_BOUND`, `DATABASE_CPU_BOUND`, `DATABASE_IO_BOUND`,
`CONNECTION_POOL_BOUND`, `MEMORY_BOUND`, `SERIALIZATION_BOUND`,
`KEDA_SCALE_LAG`, `LOAD_GENERATOR_BOUND`, `UNKNOWN`.
`LOAD_GENERATOR_BOUND` is evaluated first and, when it fires, everything else
from that run is reported as suspect.

## Wall clock

Roughly, on the reference machine:

| | |
| --- | --- |
| D1 generation | 2 min |
| D2 | 10 min |
| D4 | 45–90 min |
| concurrency sweep, one dataset | 20–60 min depending on where it saturates |
| result formats, 2 classes x 6 formats x 3 reps | ~50 min |
| `profile` (ladder, six rungs, three 10-min passes, two chart upgrades) | ~65 min |
| `serialize` (no cluster) | 15 s |
| fixed replica scaling | ~2 h for 4 counts x 6 rates |
| worker sweep (`benchmark-workers`) | ~3–5 h for the 3 worker counts x 4 replica counts grid |
| KEDA K1–K7 | ~1.5 h |
| `benchmark-full` on all four datasets | 25–40 h |

`benchmark-full` is an overnight-and-then-some run. It is resumable for exactly
that reason.
