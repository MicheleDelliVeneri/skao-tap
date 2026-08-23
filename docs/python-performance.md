# Python performance profiling

The Python performance workflow benchmarks CPU-bound hot paths on pushes to
`main`, relevant pull requests, and manual dispatches. It uses CodSpeed's
simulation mode to provide repeatable comparisons and flamegraphs.

The suite currently measures:

- ADQL geometry translation;
- ADQL translation plus referenced-table inspection;
- VOTable serialization of 1,000 typed rows;
- JSON serialization of 1,000 typed rows.

These tests intentionally exclude PostgreSQL, network traffic, and disk I/O.
That keeps pull-request comparisons deterministic. Database capacity is tested
by the separate PostgreSQL performance workflow.

## Run locally

Create the project environment if you do not have one, install the benchmark
plugin into it, and run:

```console
uv sync --all-groups
uv pip install --python .venv/bin/python pytest-codspeed==5.0.3
uv run --no-sync pytest tests/benchmarks -v --codspeed
```

`--no-sync` is what keeps the plugin installed: a plain `uv run` would restore
the environment to the lockfile and drop it again. Without the plugin the
benchmark module skips itself rather than failing.

A result is a regression signal, not an application service-level objective.
Investigate meaningful changes in the CodSpeed comparison and flamegraph before
changing a merge policy. New benchmarks should use representative fixed input
and avoid clocks, randomness, network calls, and database access.
