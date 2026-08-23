# Db Scaling — 20260823T184745Z

Run `20260823T184745Z-b16f52f5-db-scaling` · commit `a309863d585b` (working tree dirty) · 2026-08-23 22:01 UTC

!!! success "Validity guards passed"
    No swapping, no OOM kills, no unexpected restarts, disk headroom
    kept, load generator below its own ceiling, monitoring coverage
    complete.

## Headline

| finding | value | evidence |
| --- | --- | --- |
| peak single-replica throughput | **230.7** | conc-D1-c4-r1 at 4 clients, p95 28 ms |
| sustainable single-replica capacity (C1) | **230.7** | highest successful rps with p95 within the 2.0s SLO and errors under 1% |

## What was measured

| dataset | size | ObsCore rows | total rows | index/table | generation |
| --- | --- | --- | --- | --- | --- |
| D1 | 2.06 GiB | 600,120 | 8,624,396 | 0.63 | 2 s |
| D2 | 10.26 GiB | 3,000,213 | 43,120,494 | 0.63 | 517 s |

Sizes are `pg_database_size()` with indexes — not row counts times an
assumed row width. One database is grown through every target, so each
tier is a genuine prefix of the next.

## Throughput and latency against concurrency

| dataset | clients | reps | requests/s | p95 (ms) | errors | bottleneck |
| --- | --- | --- | --- | --- | --- | --- |
| D1 | 1 | 3 | 177.0 ± 1.3 | 12 | 0.00% | `UNKNOWN` |
| D1 | 2 | 3 | 224.9 ± 7.2 | 15 | 0.00% | `UNKNOWN` |
| D1 | 4 | 8 | 223.0 ± 5.2 | 29 | 0.00% | `TAP_CPU_BOUND` |
| D2 | 1 | 3 | 144.4 ± 3.1 | 13 | 0.00% | `UNKNOWN` |
| D2 | 2 | 3 | 169.6 ± 5.2 | 20 | 0.00% | `UNKNOWN` |
| D2 | 4 | 3 | 195.6 ± 6.1 | 33 | 0.00% | `TAP_CPU_BOUND` |
| D2 | 8 | 8 | 184.9 ± 5.4 | 66 | 0.00% | `TAP_CPU_BOUND` |

Intervals are 95% Student-t across repetitions. The sweep stops when two
saturation signals agree, so the last row of a dataset is where that
dataset's ceiling was found rather than where the ladder ran out.

## Where the limit was

| classification | measurements | meaning |
| --- | --- | --- |
| `TAP_CPU_BOUND` | 25 | The API's own CPU is the constraint: it sat at its ceiling, or was CFS-throttled, for a material part of the window. The ceiling compared against is min(pod CPU limit, workers), not the pod limit: ADQL translation is pure-Python and holds the GIL, so one worker cannot exceed one core whatever the cgroup allows. Relieved by more processes (tapApi.workers) or more pods, never by a bigger limit on one worker. |
| `UNKNOWN` | 12 | Nothing saturated by these rules. Either the offered load was below every ceiling, or the limit is somewhere this suite does not instrument — which is itself worth knowing. |
| `DATABASE_CPU_BOUND` | 2 | PostgreSQL is CPU-bound. Adding API replicas cannot help and will make it worse; this needs cheaper plans, fewer rows, or a bigger database. |
| `SERIALIZATION_BOUND` | 2 | Large responses with a busy API and an idle database: the time is going into formatting and streaming rows, not into finding them. This is a result-pipeline cost, not a query cost. |

## Query plans

| flag | plans | why it matters |
| --- | --- | --- |
| `bad_cardinality_estimate` | 14 | The planner's row estimate was an order of magnitude out, which is how good indexes get ignored. |
| `large_rows_removed_by_filter` | 1 | The plan fetched rows only to throw them away - work done for nothing, and usually a missing or wrong-ordered index. |

## Graphs

### Throughput against offered concurrency

![Throughput against offered concurrency](plots/rps_vs_concurrency.png)

### Latency percentiles against concurrency

![Latency percentiles against concurrency](plots/latency_vs_concurrency.png)

### Errors against concurrency

![Errors against concurrency](plots/errors_vs_concurrency.png)

### Throughput against database size

![Throughput against database size](plots/rps_vs_size.png)

### Latency against database size

![Latency against database size](plots/latency_vs_size.png)

### Buffer cache hit ratio against database size

![Buffer cache hit ratio against database size](plots/cache_hit_vs_size.png)

### API CPU against throughput

![API CPU against throughput](plots/tap_cpu_vs_throughput.png)

### PostgreSQL CPU against throughput

![PostgreSQL CPU against throughput](plots/postgres_cpu_vs_throughput.png)

### PostgreSQL read I/O against throughput

![PostgreSQL read I/O against throughput](plots/postgres_io_vs_throughput.png)

### Throughput by query class

![Throughput by query class](plots/query_class_rps.png)

### Latency by query class

![Latency by query class](plots/query_class_latency.png)

### Query class against database size

![Query class against database size](plots/class_size_heatmap.png)

### Latency against response size

![Latency against response size](plots/result_size_vs_latency.png)

### Run-to-run variability

![Run-to-run variability](plots/run_to_run_variability.png)

## Environment

| property | value |
| --- | --- |
| host | macOS-26.6.2-arm64-arm-64bit-Mach-O (14 CPUs) |
| Kubernetes | v1.36.1 |
| node capacity | 14 CPU, 16354760Ki |
| KEDA | ghcr.io/kedacore/keda:2.18.1 |
| PostgreSQL | PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit |
| extensions | pg_sphere 1.5.2, pg_stat_statements 1.12, plpgsql 1.0 |
| seed | 20260823 |
| corpus sha256 | `483dc27a6f2749a7` |
| chart values sha256 | `b6e9c1b32e19c965` |


[Download the per-measurement CSV](summary.csv) ·
[environment.json](environment.json) · [dataset.json](dataset.json)

Raw per-request samples and Prometheus series stay with the run that
produced them, under `benchmarks/tap-performance/results/20260823T184745Z-b16f52f5-db-scaling/`:
Parquet for every request and every metric, PostgreSQL statistics
before and after, `EXPLAIN` plans, and the exact ScaledObject and HPA
YAML the measurement ran against.
