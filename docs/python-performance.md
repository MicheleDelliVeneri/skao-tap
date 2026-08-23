# Python performance profiling

The Python performance workflow benchmarks CPU-bound hot paths on pushes to
`main`, relevant pull requests, and manual dispatches. It stores both
machine-readable benchmark results and a Python flamegraph as GitHub artifacts
for 30 days.

The suite currently measures:

- ADQL geometry translation;
- ADQL translation plus referenced-table inspection;
- VOTable serialization of 1,000 typed rows;
- JSON serialization of 1,000 typed rows.

These tests intentionally exclude PostgreSQL, network traffic, and disk I/O.
That makes their benchmark JSON useful for comparisons between runs. Database
and full-service capacity are covered by the PostgreSQL performance workflow.

## Run locally

Install the benchmark and profiling tools into the project environment:

```console
uv sync --locked --all-groups
uv pip install --python .venv/bin/python pytest-benchmark==5.2.3 py-spy==0.4.2
uv run --no-sync pytest tests/benchmarks -v --benchmark-json=benchmarks.json
py-spy record --output flamegraph.svg --format flamegraph -- \
  .venv/bin/python -m pytest tests/benchmarks -q --benchmark-only
```

The benchmark JSON shows distributions and relative speeds. Open the SVG
flamegraph and look for the widest application frames: those functions consume
the largest share of sampled Python time and are the first candidates for
optimisation.

A result is a regression signal, not an application service-level objective.
New benchmarks should use representative fixed input and avoid clocks,
randomness, network calls, and database access.
