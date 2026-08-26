# Python performance profiling

The Python performance workflow benchmarks CPU-bound hot paths on pushes to
`main`, relevant pull requests, and manual dispatches. It stores both
machine-readable benchmark results and a Python flamegraph as GitHub artifacts
for 30 days.

The suite currently measures:

- ADQL geometry translation;
- ADQL translation plus referenced-table inspection;
- VOTable serialization of 1,000 typed rows;
- JSON serialization of 1,000 typed rows;
- a whole `/tap/sync` request, per query class and over the normal mix.

These tests intentionally exclude PostgreSQL, network traffic, and disk I/O.
That makes their benchmark JSON useful for comparisons between runs. Database
and full-service capacity are covered by the PostgreSQL performance workflow.

## The whole-request benchmarks

The first four measure functions. Everything else a request pays for — DALI
parameter parsing, the published-table check, format negotiation, the streamed
response, the observability instrumentation — used to be invisible to this
suite, which is how the service's CPU ceiling kept an attribution to ADQL
translation for months after the fast path took translation from 41 ms to
1.2 ms.

`test_benchmark_sync_request_*` drives the real ASGI application the way
uvicorn does: a scope, a `receive` that hands over the form body, a `send`
that collects the response. Two things are deliberately absent, and both are
named subsystems in the cluster profile
(`benchmarks/egernia-performance`, `make benchmark-profile`), so the gap
between this figure and a measured per-request CPU is attributable rather than
mysterious:

- **PostgreSQL, and libpq with it.** The connection is stubbed and its rows are
  built once at import, so psycopg's per-request row conversion is excluded
  rather than imitated. A Python re-implementation of a C conversion would be a
  fabricated cost, and a wrong one.
- **uvicorn, h11 and the socket.** The application is called directly, so the
  HTTP parse and the write are absent.

Row counts and shapes per class are the ones a D1 saturation window actually
measured — the mean successful response divided by that class's row width — not
the `TOP` clause. A cone search with `TOP 500` returns around 233 rows, and
sizing the writers by the limit would make every cone class several times its
measured weight. `test_benchmark_sync_request_normal_mix` draws one request per
iteration from the mix in `config/scenarios.yaml`'s proportions, so its **mean**
is the mix-weighted per-request cost: at `replicas: 1, workers: 1` the service
serves one request at a time, so the reciprocal of that mean is comparable to a
measured single-worker saturation throughput. The comparison is a sanity check
on the benchmark's scale, not a service-level objective.

## Run locally

Install the benchmark and profiling tools into the project environment:

```console
uv sync --locked --all-groups
uv pip install --python .venv/bin/python pytest-benchmark==5.2.3 py-spy==0.4.2
uv run --no-sync pytest tests/benchmarks -v --benchmark-json=benchmarks.json
py-spy record --output flamegraph.svg --format flamegraph --nonblocking -- \
  .venv/bin/python -m pytest tests/benchmarks -q --benchmark-only
```

`--nonblocking` matters more than it looks. py-spy's default pauses the process
to walk its stacks; against a suite that drives an ASGI app with an event loop
and a threadpool that turns seconds of benchmarking into hours — measured at
2h29m against 15s for the same 19s of work. It is the same effect that costs a
saturated `tap-api` worker 74% of its throughput when profiled the same way.
The cost of avoiding it is torn stacks: py-spy discards the reads it can detect
as inconsistent, roughly half of them here, which leaves a flamegraph thin on
samples but still showing the widest frames. For per-subsystem shares rather
than a visual scan, use the cluster profile (`make benchmark-profile`), which
samples one worker for ten minutes.

The benchmark JSON shows distributions and relative speeds. Open the SVG
flamegraph and look for the widest application frames: those functions consume
the largest share of sampled Python time and are the first candidates for
optimisation.

A result is a regression signal, not an application service-level objective.
New benchmarks should use representative fixed input and avoid clocks,
randomness, network calls, and database access.
