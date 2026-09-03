# PostgreSQL and TAP performance testing

The PostgreSQL performance workflow runs every Monday and on manual dispatch.
It is intentionally separate from the pull-request correctness gate because
large data generation and wall-clock load tests are noisy on shared runners.

The default run loads one million deterministic synthetic sky sources and
tests 1, 8, 16, and 32 concurrent clients for 60 seconds at each level.

## Two measurements

The database-only sweep uses weighted pgbench scripts:

- 70% indexed source-ID lookups;
- 30% pg_sphere cone searches through a GiST index.

The end-to-end sweep sends a representative mix to TAP /sync:

- metadata queries;
- indexed point lookups;
- cone searches;
- 1,000-row streamed results.

Running both distinguishes PostgreSQL query cost from ADQL translation,
connection-pool waiting, result serialization, and HTTP streaming.

## Actionable output

Each run writes a bottleneck summary to the GitHub Actions job summary and
uploads the full evidence for 30 days:

- per-concurrency throughput, p50/p95/p99 latency, and error rate;
- an automated saturation warning when latency rises without useful throughput
  growth;
- pg_stat_statements rankings by total and mean execution time;
- shared-buffer hit ratios, temporary-block spills, and I/O timing;
- a JSON EXPLAIN ANALYZE plan with buffer usage for the cone search;
- PostgreSQL waits, container statistics, and service/database logs;
- a py-spy flamegraph of the API under eight-client load.

Use the flamegraph to find wide application frames, then use the SQL ranking
and JSON plan to determine whether the time is in Python, connection-pool
waiting, serialization, a scan, a bad cardinality estimate, or storage I/O.

## Interpreting the concurrency knee

The current pool is limited to eight connections per service process. A
streamed result retains a checked-out connection until the client finishes
reading it. A throughput plateau and steep p95 increase above eight clients
therefore suggests pool saturation when PostgreSQL CPU and I/O remain
available.

For a deployment, keep the potential connection budget below the server limit:

    API replicas * API workers * API pool max
  + executor replicas * executor workers * executor pool max
  < PostgreSQL max_connections - administrative reserve

Repeat final capacity measurements on a stable runner or production-like
environment. GitHub-hosted results are suitable for finding bottlenecks and
large regressions, not for declaring a production service-level objective.

## Manual runs

Choose Actions, PostgreSQL performance, Run workflow. Start with the defaults.
For dataset scaling, repeat with 100000, 1000000, and 10000000 rows without
changing the workload or duration, and compare the downloaded summaries.

## Sizing the server to its container

The postgres image's defaults are sized for a laptop: 128MB of shared
buffers and eight parallel workers for the whole cluster. Left in place they
cost the comparison benchmark its aggregation class (tap-compare Q13, a
GROUP BY over the 500,096-row ObsCore view, run 20260902T163638Z): 8.3
requests/s from 4 clients upward, and a p95 of 1.83 s at 8 clients where
DaCHS managed 11.0 requests/s and 1.07 s on the same 8 CPUs and 8 GiB.

Two independent causes, measured on an isolated copy of the pinned stack
(3 x 60 s windows per point, 95% confidence intervals):

- **The parallel budget starves concurrent queries.** The planner gives a
  full-table aggregate two workers regardless of load, but the cluster cap
  `max_parallel_workers = 8` covers only four of the eight queries the API
  pool admits; the rest run a parallel-shaped plan with the leader alone.
  Sampling `pg_stat_activity` at 8 clients showed 6.2 workers busy on
  average (max 7 of 8) and a bimodal latency (0.34 s to 1.84 s). Raising
  the cap so that every pooled query can get its planned workers made the
  latency unimodal — p95 at 8 clients 1.83 s to 1.10 s, at 32 clients
  4.73 s to 4.16 s — at unchanged throughput. Disabling parallelism instead
  gains 6% throughput at 8 clients but doubles single-user latency
  (0.20 s to 0.42 s); four workers per gather helps only an idle server.
- **The table does not fit shared_buffers.** The 559 MB view is streamed
  from the OS page cache through 128MB of shared buffers on every query
  (71,755 buffer reads of 72,398). At `shared_buffers = 1GB` every read is
  a hit, and the same query costs 40% less CPU: 11.6 requests/s at 8
  clients (12.0 with the stock parallel cap), 11.2 to 11.5 from 4 to 32
  clients, p95 0.97 s at 8 clients, p99 3.3 s at 32 clients against a 5 s
  pool timeout. `work_mem` did not matter for this class (its hash is
  1.4 MB, one batch); the collation of the text group keys did not either
  (`COLLATE "C"` on the keys: 7 warm runs each, within noise at 2 and at 0
  workers per gather).

| Q13, csv, 8 CPUs | c=1 | c=4 | c=8 | c=16 | c=32 |
|---|---|---|---|---|---|
| stock: rps | 4.92 | 8.27 | 8.31 | 8.29 | 7.99 |
| stock: p95 s | 0.28 | 0.71 | 1.83 | 2.83 | 4.73 |
| workers 16: rps | 5.05 | 8.31 | 8.16 | 8.07 | 7.79 |
| workers 16: p95 s | 0.28 | 0.59 | 1.10 | 2.13 | 4.16 |
| + shared_buffers 1GB: rps | 5.40 | 11.47 | 11.63 | 11.44 | 11.18 |
| + shared_buffers 1GB: p95 s | 0.27 | 0.49 | 0.97 | 1.71 | 3.13 |

The rule the shipped settings follow — `docker-compose.yml` for the 4 GiB
the comparison pins give the container, `postgresql.tuning` in the chart
for its 2 GiB pod — and which any other deployment should re-apply to its
own limits:

- `shared_buffers` = 1/4 of the container's memory limit. The rest is the
  OS page cache, per-backend `work_mem`, and the parallel workers.
- `effective_cache_size` = 3/4 of the memory limit. A planner hint only;
  the image's 4GB default already matches the compose budget.
- `max_parallel_workers` >= (sum of the services' connection pool sizes) x
  `max_parallel_workers_per_gather`. Every pooled query must be able to
  get the workers its plan assumed. Compose: (API 8 + executor 8) x 2 = 32.
- `max_worker_processes` = `max_parallel_workers` + 8, for the server's own
  launchers, I/O workers and autovacuum, which come out of the same pool.
- Leave `max_parallel_workers_per_gather` at 2; the sync path's
  `SET LOCAL jit = off` stays as it is.

These are cluster-wide, restart-only settings (`max_worker_processes` in
particular), so they cannot be applied per query with `SET LOCAL`; they are
server arguments. `tests/unit/test_postgres_sizing.py` pins both the values
and the rule.
