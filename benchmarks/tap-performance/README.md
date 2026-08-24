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
tapbench/
  cluster.py    cluster lifecycle and the resource caps
  corpus.py     Q01-Q14 and the deterministic parameter corpus
  runs.py       run directories, provenance, resumability
  dataset/      schema, server-side generator, size-driven growth
  load/         closed-loop and open-loop generation, per-request samples
  collect/      Prometheus, PostgreSQL statistics, Kubernetes, validity guards
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

The schema is five CAOM levels plus ObsCore at plane level, with the wide text
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
| Q08 | Observation→Plane join | the CAOM join clients actually make |
| Q09 | four-level CAOM join | join depth with fan-out (stress) |
| Q10 | ~1,000 rows | serialisation and streaming |
| Q11 | ~10,000 rows | where the response body dominates (stress, and the class the result-format family runs) |
| Q12 | empty spatial result | the cost of finding nothing |
| Q13 | aggregation | full scan and hash, tiny answer (stress) |
| Q14 | deliberately expensive | five-way join, unanchored LIKE, forced sort (stress) |

The normal mix is Q01 5%, Q02 15%, Q03 15%, Q04 10%, Q05 25%, Q06 10%,
Q07 10%, Q08 5%, Q10 5%. Q09/Q11/Q13/Q14 run on their own: one Q14 in a
hundred requests moves the p99 of everything else and tells you nothing about
either. `tapbench stress` runs just those four, each at fixed concurrency —
minutes instead of a whole family, for a change aimed at one expensive class.

## Load generation

**Closed loop** (N clients, each issuing the next request when the last
returns) for capacity and latency at a known parallelism. **Open loop**
(Poisson arrivals at a rate, whether or not the service keeps up) for anything
involving a queue — a closed-loop client cannot overload anything, because it
slows down exactly as much as the service does. `t_offered` is recorded
alongside `t_start`, so the generator's own lateness is measurable rather than
absorbed into the service's latency.

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
tapbench result-formats     Q10 and Q11 through all six writers, fixed concurrency
tapbench serialize          the writers in process: no cluster, no database, no HTTP
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
| `serialize` (no cluster) | 15 s |
| fixed replica scaling | ~2 h for 4 counts x 6 rates |
| KEDA K1–K7 | ~1.5 h |
| `benchmark-full` on all four datasets | 25–40 h |

`benchmark-full` is an overnight-and-then-some run. It is resumable for exactly
that reason.
