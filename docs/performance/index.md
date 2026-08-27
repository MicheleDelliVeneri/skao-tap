# Performance

Archived benchmark results. They were produced by
`benchmarks/egernia-performance`, a cluster benchmark harness this repository
used to carry, which measured the service, PostgreSQL as the data grows,
replica scaling and autoscaling behaviour separately — because they fail
separately.

That harness has been removed in favour of the timing guards in the
integration suite and the CPU microbenchmarks (see
[Performance testing](../benchmarking.md)). The measurements below stand as
records; the `make benchmark-*` targets and `egernia_bench` commands the pages
reference no longer exist, and the harness itself is in the git history.

Each run below is published in full: the graphs, the per-measurement CSV,
and the provenance needed to know whether two runs are comparable at all
(commit, image ids, seed, corpus hash, chart values hash). Runs
accumulate rather than replace, so a regression has somewhere to show up.

## Latest

[20260825T180219Z-f8b21fc4-worker-sweep](20260825T180219Z-f8b21fc4-worker-sweep/index.md) · [CSV](20260825T180219Z-f8b21fc4-worker-sweep/summary.csv)

## Latest by family

| family | run | datasets |
| --- | --- | --- |
| `db-scaling` | [20260825T005436Z-b450b0a9-db-scaling](20260825T005436Z-b450b0a9-db-scaling/index.md) | D1 D2 D3 D4 |
| `fixed-scaling` | [20260824T014320Z-a5058118-fixed-scaling](20260824T014320Z-a5058118-fixed-scaling/index.md) | D2 |
| `keda` | [20260824T140134Z-bf7e4b24-keda](20260824T140134Z-bf7e4b24-keda/index.md) | D1 |
| `profile` | [20260825T163627Z-034d69ba-profile](20260825T163627Z-034d69ba-profile/index.md) | D1 |
| `worker-sweep` | [20260825T180219Z-f8b21fc4-worker-sweep](20260825T180219Z-f8b21fc4-worker-sweep/index.md) | D1 |

## All runs

- [20260825T180219Z-f8b21fc4-worker-sweep](20260825T180219Z-f8b21fc4-worker-sweep/index.md)
- [20260825T163627Z-034d69ba-profile](20260825T163627Z-034d69ba-profile/index.md)
- [20260825T005436Z-b450b0a9-db-scaling](20260825T005436Z-b450b0a9-db-scaling/index.md)
- [20260824T140134Z-bf7e4b24-keda](20260824T140134Z-bf7e4b24-keda/index.md)
- [20260824T102832Z-29507cbb-keda](20260824T102832Z-29507cbb-keda/index.md)
- [20260824T074332Z-b4fa9d64-keda](20260824T074332Z-b4fa9d64-keda/index.md)
- [20260824T014320Z-a5058118-fixed-scaling](20260824T014320Z-a5058118-fixed-scaling/index.md)
- [20260823T231057Z-f0cd5bd7-db-scaling](20260823T231057Z-f0cd5bd7-db-scaling/index.md)
- [20260823T184745Z-b16f52f5-db-scaling](20260823T184745Z-b16f52f5-db-scaling/index.md)

## Reading these numbers

- A figure without an interval is one measurement. Intervals across
  repetitions are 95% Student-t; percentile intervals within a run are
  percentile bootstrap.
- `LOAD_GENERATOR_BOUND` on any measurement means the client was the
  limit, and nothing else from that measurement describes the service.
- A run marked invalid is published anyway, with the reason. The samples
  are the evidence of what went wrong.
- Two throughputs closer together than the run-to-run variability plot
  shows are the same throughput.
