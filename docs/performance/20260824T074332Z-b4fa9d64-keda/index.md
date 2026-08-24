# Keda — 20260824T074332Z

Run `20260824T074332Z-b4fa9d64-keda` · commit `1a99bdad4305` · 2026-08-24 09:54 UTC

!!! danger "This run is marked invalid"
    keda-K3: load_generator_offered_the_rate; keda-K4: load_generator_offered_the_rate; keda-K6: load_generator_offered_the_rate; keda-K7: load_generator_offered_the_rate

    The numbers below are kept as evidence of what went wrong. They do
    not describe the service under the conditions intended.

## Headline

| finding | value | evidence |
| --- | --- | --- |
| peak single-replica throughput | **191.5** | conc-D2-c4-r0 at 4 clients, p95 34 ms |
| sustainable single-replica capacity (C1) | **191.5** | highest successful rps over valid measurements with p95 within the 2.0s SLO and errors under 1% |

## What was measured

| dataset | size | ObsCore rows | total rows | index/table | generation |
| --- | --- | --- | --- | --- | --- |
| D2 | 25.28 GiB | 7,399,024 | 106,348,439 | 0.63 | 13 s |

Sizes are `pg_database_size()` with indexes — not row counts times an
assumed row width. One database is grown through every target, so each
tier is a genuine prefix of the next.

## Throughput and latency against concurrency

| dataset | clients | reps | requests/s | p95 (ms) | errors | bottleneck |
| --- | --- | --- | --- | --- | --- | --- |
| D2 | 1 | 1 | 113.2 | 19 | 0.00% | `MEMORY_BOUND` |
| D2 | 2 | 1 | 176.2 | 20 | 0.00% | `MEMORY_BOUND` |
| D2 | 4 | 1 | 191.5 | 34 | 0.00% | `TAP_CPU_BOUND` |

Intervals are 95% Student-t across repetitions. The sweep stops when two
saturation signals agree, so the last row of a dataset is where that
dataset's ceiling was found rather than where the ladder ran out.

## Autoscaling

| scenario | profile | detect (s) | HPA (s) | provision (s) | routing (s) | total (s) | recovery (s) | peak | events | reversals |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **K1** | idle to 0.5*C1 | — | — | — | — | — | 26.4 | 1 | 0 | 0 |
| **K2** | 0.5*C1 step to 3.5*C1 | 104.0 | 14.7 | 1.0 | 113.1 | 231.2 | — | 3 | 2 | 0 |
| **K3** ⚠ | 0 to 6*C1 spike | 92.0 | — | 1.1 | — | — | — | 3 | 6 | 3 |
| **K4** ⚠ | 0.5*C1 to 6*C1 gradual 10-minute ramp | 254.0 | 18.5 | 1.0 | 84.4 | 356.1 | 25.6 | 3 | 2 | 0 |
| **K5** | alternating 0.5*C1 and 4*C1 every 2 minutes | 168.0 | — | 1.0 | 25.6 | 220.7 | — | 3 | 3 | 1 |
| **K6** ⚠ | sustained 6*C1 for 15 minutes | 108.0 | 138.6 | 1.0 | — | — | — | 3 | 3 | 2 |
| **K7** ⚠ | 4*C1 to 0.2*C1 recovery | 156.0 | 265.5 | — | — | — | 185.6 | 3 | 6 | 2 |

Stages: **detect** is the scaler's metric crossing its threshold,
**HPA** the replica request changing, **provision** Pod creation to
Ready, **routing** Ready to serving traffic, **recovery** the load
change to p95 back inside the SLO. A dash means the stage could not
be established from the evidence — recorded rather than estimated.
A ⚠ on the scenario means one of its validity guards failed, so its
timings describe conditions other than the ones intended.

## Where the limit was

| classification | measurements | meaning |
| --- | --- | --- |
| `DATABASE_IO_BOUND` | 10 | The working set does not fit in shared_buffers plus page cache: the database is fetching from disk. This is the class that appears as the dataset grows and is the reason the size sweep exists. |
| `MEMORY_BOUND` | 4 | A container approached or exceeded its memory limit. An OOM kill invalidates the run outright; sustained pressure below it still distorts latency through reclaim. |
| `TAP_CPU_BOUND` | 1 | The API's own CPU is the constraint: it sat at its ceiling, or was CFS-throttled, for a material part of the window. The ceiling compared against is min(pod CPU limit, workers), not the pod limit: ADQL translation is pure-Python and holds the GIL, so one worker cannot exceed one core whatever the cgroup allows. Relieved by more processes (tapApi.workers) or more pods, never by a bigger limit on one worker. |

## Graphs

### Throughput against offered concurrency

![Throughput against offered concurrency](plots/rps_vs_concurrency.png)

### Latency percentiles against concurrency

![Latency percentiles against concurrency](plots/latency_vs_concurrency.png)

### Errors against concurrency

![Errors against concurrency](plots/errors_vs_concurrency.png)

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

### Latency against response size

![Latency against response size](plots/result_size_vs_latency.png)

### Autoscaling timeline — K1

![Autoscaling timeline — K1](plots/keda_K1.png)

### Autoscaling timeline — K2

![Autoscaling timeline — K2](plots/keda_K2.png)

### Autoscaling timeline — K3

![Autoscaling timeline — K3](plots/keda_K3.png)

### Autoscaling timeline — K4

![Autoscaling timeline — K4](plots/keda_K4.png)

### Autoscaling timeline — K5

![Autoscaling timeline — K5](plots/keda_K5.png)

### Autoscaling timeline — K6

![Autoscaling timeline — K6](plots/keda_K6.png)

### Autoscaling timeline — K7

![Autoscaling timeline — K7](plots/keda_K7.png)

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
| corpus sha256 | `c02caf7c44edae77` |
| chart values sha256 | `b6e9c1b32e19c965` |


[Download the per-measurement CSV](summary.csv) ·
[environment.json](environment.json) · [dataset.json](dataset.json)

Raw per-request samples and Prometheus series stay with the run that
produced them, under `benchmarks/tap-performance/results/20260824T074332Z-b4fa9d64-keda/`:
Parquet for every request and every metric, PostgreSQL statistics
before and after, `EXPLAIN` plans, and the exact ScaledObject and HPA
YAML the measurement ran against.
