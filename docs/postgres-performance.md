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
