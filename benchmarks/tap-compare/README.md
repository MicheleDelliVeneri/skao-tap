# tap-compare: a same-hardware TAP-server benchmark

A harness for comparing IVOA TAP 1.1 servers — egernia against other open
implementations (GAVO DaCHS next, CADC `opencadc/tap` after) — on **identical
hardware over an identical logical corpus**, each server deployed per its own
project's documentation. It doubles as egernia's own end-to-end capacity
measurement, succeeding the removed cluster harness (recoverable at
`git show f6fbeb3^:benchmarks/egernia-performance/`), from which the corpus,
runner and statistics here descend.

## Running (current scope: egernia only)

```bash
# 1. the target: the repo's compose stack, seeded to the D1 corpus
docker compose up -d
TAP_DATABASE_URL=postgresql://tap:tap@localhost:5432/tap \
  PYTHONPATH=dataset uv run --group dataset python -m egernia_dataset.seed

# 2. a shake-out run (not a measurement)
uv run --group tap-compare python benchmarks/tap-compare run \
    --target egernia-local --scenario smoke

# 3. the capacity ladder
uv run --group tap-compare python benchmarks/tap-compare run \
    --target egernia-local --scenario ladder

# unit tests
uv run --group tap-compare pytest benchmarks/tap-compare/tests -q
```

Results land in `benchmarks/tap-compare/results/<UTCstamp>-<sha>-<scenario>-<target>/`:
per-rung Parquet samples (every request, with TTFB), per-rung JSON summaries
with bootstrap confidence intervals, `corpus.json`, and `environment.json`
provenance. Runs never overwrite and are resumable (`--resume <run-name>`).

## Fairness rules (lane A — the only lane)

This harness is **never pointed at a production service someone else
operates**. Every target is deployed locally by the operator, and:

- identical *logical* rows in every server (the exported `ivoa.obscore` view,
  sha256-pinned), loaded through each server's own documented ingest path —
  each server keeps its native physical layout and indexes, because a server
  *is* its recommended layout;
- identical container CPU/memory limits, one target stack running at a time;
- TLS off, auth off, HTTP/1.1 keep-alive on, the same client harness for all;
- `RESPONSEFORMAT` pinned to VOTable and CSV for cross-server rungs
  (egernia's parquet/arrow are an egernia-only appendix, never compared);
- `MAXREC` sent explicitly on every request — server defaults differ;
- the generator watches its own CPU and a rung where it ran hot is marked
  invalid, not believed;
- before any timed rung (from the DaCHS phase on): a `stilts taplint`
  conformance gate and a cross-server agreement gate (same query → same row
  count and checksum), so "same data, conformant servers" is verified rather
  than assumed.

## Claims policy

Results may claim the relative behaviour *of the measured versions, on this
hardware, on this corpus, as deployed by their own documentation, under the
recorded resource pins* — with confidence intervals, per query class, ties
reported as ties (overlapping 95% CIs, or under 10% rps / 20% p95 apart).
They may not claim anything about other corpus sizes, other hardware, other
versions, or classes a gate excluded. Classes where egernia loses are
reported with the same prominence as classes where it wins.
