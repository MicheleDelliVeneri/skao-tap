# Performance testing

Performance is checked in two places, and neither of them is a benchmark suite
in this repository any more.

## Timing guards in the integration suite

`tests/integration/test_performance.py` runs against a deployed service in the
SRC integration environment and asserts that a handful of queries finish inside
a budget. Deliberately coarse: a shared cluster has neighbours, so every number
measured there is a fact about the cluster that afternoon rather than about the
service. Each budget sits an order of magnitude above the expected time, which
is enough to catch the failures that change the shape of a query plan:

- a GiST footprint index the seeder dropped and never rebuilt, turning a cone
  search into a sequential scan;
- a b-tree lost in a migration, doing the same to a keyed lookup;
- a connection pool sized so requests queue rather than run;
- a result writer that buffers a whole table before emitting a byte.

Each of those costs a factor rather than a few percent. A regression smaller
than that is one only dedicated hardware could honestly detect, and these tests
do not pretend to.

```bash
# in the deployment stack
stack test egernia

# or against any deployment
EGERNIA_RUN_INTEGRATION_TESTS=1 EGERNIA_URL=http://egernia.test \
  uv run pytest tests/integration -v
```

Every budget is overridable — `EGERNIA_BUDGET_POINT_S`,
`EGERNIA_BUDGET_CONE_S`, `EGERNIA_BUDGET_AGGREGATE_S`,
`EGERNIA_BUDGET_ASYNC_S` — because "generous" depends on the hardware the
environment happens to run on. Each test prints its measured time whether it
passes or fails, so the log shows a trend rather than only a breach.

## CPU microbenchmarks

`tests/benchmarks/test_hot_paths.py` measures the hot paths in process, with no
cluster involved, via `pytest-benchmark`. Run by
[`python-performance.yml`](python-performance.md); these are the numbers that
say where a request's CPU goes.

```bash
uv run --group microbenchmark pytest tests/benchmarks --benchmark-only
```

## The dataset

Both need something to query. `dataset/` generates the ODP hierarchy and the
software catalogue deterministically and resumably, and the deployment stack
runs it as egernia's post-deploy job:

```bash
TAP_DATABASE_URL=postgresql://... TARGET_PRODUCTS=500000 \
  python -m egernia_dataset.seed
```

Row-driven rather than size-driven. Two limits are properties of the model
rather than knobs: one project expands to 4 observations, 8 scheduling blocks,
16 execution blocks, 128 data products and 256 artifacts, so the ODP tables
cannot hold equal row counts; and the software catalogue's
`{publisher}:{tool}:{semver}` identity admits 7,700 distinct uris and no more.

## The archived results

[Performance](performance/index.md) holds runs published by
`benchmarks/egernia-performance`, a cluster benchmark harness this repository
used to carry. It has been removed in favour of the two mechanisms above, so
those pages are records of what was measured rather than instructions you can
follow — the `make benchmark-*` targets and the `egernia_bench` CLI they name no
longer exist. The measurements stand; the harness that produced them is in the
git history.
