# Benchmarking

The numbers under [Performance](performance/index.md) come from
`benchmarks/tap-performance` in this repository. This page is what you need to
reproduce them or to judge whether they apply to your deployment.

## Reproducing a run

```bash
make benchmark-smoke                  # ~10 min: everything wired, nothing measured for long
make benchmark-db-scaling             # concurrency sweep at each dataset size
make benchmark-fixed-scaling          # replica scaling, autoscalers off
make benchmark-keda                   # autoscaling scenarios K1-K7
make benchmark-full                   # every family, every dataset
make benchmark-publish RUN=<run-dir>  # graphs and CSV into this site
```

Needs Docker, `kind`, `kubectl` and `helm`. The suite builds the images from
the working tree, brings up a single-node cluster, installs KEDA and a
Prometheus that scrapes at 2-second resolution, deploys the chart, and grows a
synthetic CAOM/ObsCore database to a measured size before measuring anything.

Every target takes `RESUME=<run-dir>` and continues where it stopped.
`benchmark-full` is a 25-40 hour run; that is why it is resumable.

## What the numbers mean

**They are hardware-specific.** Each run records the host, the node's CPU and
memory caps, the image ids, the chart values hash, the seed and the query
corpus hash. Two runs are comparable when those match and not otherwise — the
provenance is published with the graphs so this can be checked rather than
assumed.

**Capacity is quoted inside an SLO.** "C1" is the highest throughput one
replica sustained with p95 within 2 seconds and under 1% errors, not the peak
it reached. Peak throughput usually comes with a latency distribution nobody
would ship, and expressing autoscaling scenarios as multiples of an unusable
number would make them all tests of overload.

**The bottleneck classification is the actionable part.** The same flat
throughput curve is produced by a CPU-bound service, an I/O-bound database, an
exhausted connection pool and a saturated load generator, and the fix for each
is useless for the others. Each class is a rule over measured quantities and is
reported with the numbers that made it fire.

**A saturated load generator invalidates everything else.** It is checked
first, and when it fires the rest of that measurement describes the client.

## Deliberate choices worth knowing

- **Dataset sizes are `pg_database_size()`**, indexes included, not row counts
  times an assumed width. One database is grown through every target, so each
  tier is a prefix of the next rather than an independent load.
- **The corpus is deterministic and aimed at the data.** Parameters come from
  the same hash the generator used, so a cone search points where objects
  actually are — without querying first. 12,000 distinct combinations, so no
  measurement is a page-cache benchmark on one coordinate.
- **Two load shapes.** Closed loop for capacity at a known parallelism; open
  loop, Poisson arrivals, for anything involving a queue — a closed-loop client
  cannot overload anything, because it slows down exactly as much as the
  service does.
- **The API's CPU ceiling is `min(pod limit, workers)`.** ADQL translation is
  pure-Python and holds the GIL, so one worker cannot exceed one core whatever
  the cgroup allows.
- **Autoscaling scenarios drive `/tap/async`.** The chart's ScaledObject scales
  executors on queue depth, so only work that creates jobs moves the scaler
  metric.
- **Cone search needs an expression index.** The translator emits
  `spoint(RADIANS(s_ra), RADIANS(s_dec)) @ scircle(...)`, so the GiST index has
  to be on that expression — see [Autoscaling](autoscaling.md) and the suite's
  README for the full note.

## Constraints on reading and comparing results

- **Every run is pinned to a build.** The image tag is recorded with the run,
  so numbers compare only against runs of the same build: a service change
  means a new baseline, not a delta against yesterday's.
- **A sustained-overload point is not measurable open-loop.** Past capacity
  the queue grows without bound, so nothing in that region is a steady state
  to report — the suite can show *that* overload sheds and how, not a
  throughput "at" it.
- **Guards mark runs invalid rather than deleting them.** Read the run's
  `invalid.json` before concluding the harness broke: the run is there, with
  the reason it cannot be trusted.
- **`tapbench reclassify` fixes analysis mistakes without re-measuring.** The
  raw measurements are kept, so a wrong bottleneck classification is corrected
  by re-running the rules, not the load.

Full method, layout and wall-clock estimates: `benchmarks/tap-performance/README.md`.
