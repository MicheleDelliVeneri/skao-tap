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
(the cluster benchmark harness, since removed), so the gap
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
iteration from the mix's proportions, so its **mean**
is the mix-weighted per-request cost: at `replicas: 1, workers: 1` the service
serves one request at a time, so the reciprocal of that mean is comparable to a
measured single-worker saturation throughput. The comparison is a sanity check
on the benchmark's scale, not a service-level objective. The mix proportions
are inlined in `tests/benchmarks/test_hot_paths.py` (they came from the removed
cluster harness's scenario configuration).

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
samples but still showing the widest frames. (Per-subsystem shares used to come
from the cluster harness's ten-minute worker profile; that harness has been
removed — see [the performance archive](performance/index.md).)

The benchmark JSON shows distributions and relative speeds. Open the SVG
flamegraph and look for the widest application frames: those functions consume
the largest share of sampled Python time and are the first candidates for
optimisation.

A result is a regression signal, not an application service-level objective.
New benchmarks should use representative fixed input and avoid clocks,
randomness, network calls, and database access.

## Measurements retired from source comments

Point-in-time figures that used to live in code comments. The invariants they
motivated remain in the code; the numbers are provenance, kept here so a
comment cannot silently go stale. All were taken on the development corpus
around commit `29b8aa6` (2026-08) unless a run directory says otherwise.

- **ADQL translation** (`egernia_core/query/adql.py`): translation was 41 ms of
  a ~50 ms request before the one-parse + SLL-first changes; ANTLR full-context
  prediction was 71% of the profile (~42,000 closure operations per query).
  After: ~1.2 ms on the fast path (~35x). A translation-cache miss holds its
  window open ~2.5 ms; the thread-local outcome flag replaced a
  `cache_info()` delta that misreported ~1 hit in 8,000 at a 0.7% miss rate.
- **Translation cache sizing** (`egernia_core/config.py`): 512 entries with
  their keys measured ~473 KiB per worker.
- **COPY DSV** (`egernia_core/query/copy_dsv.py`, reproduce with
  `tests/component/test_copy_dsv_cost.py` on a seeded ObsCore, 12 columns):
  Python writer 28.40 µs/row (of which `_csv.writer` quoting 9.19 µs/row);
  raw `COPY` (wrong bytes) 12.76 µs/row (2.22x); `COPY` + projection
  14.17 µs/row (2.00x, the shipped path — formatting costs ~1.4 µs/row).
  On a corpus of integral floats the advantage grows to 3.02x. Receiving the
  COPY bytes costs the API 0.134 µs/row (`tests/benchmarks/test_hot_paths.py`);
  the raw divergence count between the two writers was eleven, four in float8.
- **COPY VOTable** (same module, `votable_projection`; same reproduction):
  Python writer 28.81 µs/row, `COPY` + folded projection 17.66 µs/row (1.63x)
  on 20,000 seeded ObsCore rows, 12 columns. App-side, receiving and
  un-escaping the `<TR>` rows costs 0.20 ms per 1,000 rows against the writer's
  4.79 ms (`tests/benchmarks/test_hot_paths.py`). Two pre-existing DSV
  divergences were found by the VOTable probes and fixed for both formats:
  `time` values with trailing-zero fractions ('03:04:05.5' vs Python's
  '03:04:05.500000') and all-blank `char(n)` values, which `nullif(col, '')`
  turned into NULL because `''::bpchar` compares equal to blanks.
- **Probes** (`egernia_api/main.py`): with liveness pointed at
  `/tap/availability`, the API was SIGKILLed twice by its own liveness probe
  at an offered rate well inside its closed-loop capacity.
- **Pool-wait histogram** (`egernia_core/observability.py`): without a bucket
  edge at the pool timeout, a 5 s timeout was reported as a 9.7 s p95.
- **Queue metrics** (`egernia_core/observability.py`): with 1,713 jobs queued
  under steady drain, the oldest job's age saturated at 54 s — depth, not age,
  is the scaling signal.
- **Join misestimates** (`egernia_core/metadata/schema_gen.py`): the removed
  cluster benchmark measured 50x–477x join misestimates on its join-heavy
  classes (Q09, Q11, Q14); extended statistics do not fix join selectivity.
