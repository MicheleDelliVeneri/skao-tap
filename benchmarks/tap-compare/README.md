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

# 4. the comparison: DaCHS up, corpus exported, gates + interleaved rungs
scripts/export_obscore_snapshot.sh benchmarks/tap-compare/corpus
docker compose -f benchmarks/tap-compare/docker-compose.dachs.yml up -d
uv run --group tap-compare python benchmarks/tap-compare compare \
    --targets egernia-local dachs-local --scenario compare

# 5. render the report into docs/performance/<run-name>/
uv run --group tap-compare python benchmarks/tap-compare publish --run <run-name>

# unit tests
uv run --group tap-compare pytest benchmarks/tap-compare/tests -q
```

Scenarios: `compare` is the reference protocol (fixed 1–32 ladder, VOTable +
CSV, 120 s windows, 3 interleaved repetitions); `compare-demo` is the same
protocol shortened for developer hardware; `compare-smoke` proves the
pipeline and measures nothing. The gates (taplint conformance and the
cross-server agreement check) run first and a taplint failure refuses the
comparison. Comparative cells carry 95% confidence intervals and the
pre-registered tie rule: overlapping intervals, or under 10% apart in
throughput, is a tie.

Results land in `benchmarks/tap-compare/results/<UTCstamp>-<sha>-<label>/`
(the label is `<scenario>-<target>` for single-target `run`s and
`tap-compare` for multi-target `compare` runs):
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
- identical container CPU/memory limits, one target under load at a time
  (every target stack stays up so repetitions can interleave A,B,A,B);
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

## Remaining work (the runbook for the next session)

Phases 1–2 and the phase-4 machinery are done: harness, DaCHS target, gates
(both servers pass taplint; 11/11 classes agree), the `compare` command with
interleaved repetitions, and the `publish` renderer — all live-verified, the
pipeline proven end-to-end with `compare-smoke`. What remains, in order:

1. **Run the comparison on real hardware** (not a laptop). On a dedicated
   Linux box with Docker:
   ```bash
   # egernia stack + corpus
   docker compose up -d --build
   TAP_DATABASE_URL=postgresql://tap:tap@localhost:5432/tap \
     PYTHONPATH=dataset uv run --group dataset python -m egernia_dataset.seed
   scripts/export_obscore_snapshot.sh benchmarks/tap-compare/corpus
   # DaCHS (first start ingests the corpus, ~15 min)
   docker compose -f benchmarks/tap-compare/docker-compose.dachs.yml up -d --build
   # the reference comparison (hours; resumable with --resume <run-name>)
   uv run --group tap-compare python benchmarks/tap-compare compare \
       --targets egernia-local dachs-local --scenario compare
   ```
   Sanity first: `--scenario compare-smoke` end-to-end, then the real one.
   Mind the parity pins: nothing else running on the box; the DaCHS compose
   file pins cpus/mem — pin the egernia stack to the same budget (compose
   override) and record both in the report.
2. **Publish**: `... publish --run <run-name>` renders into
   `docs/performance/<run-name>/`; then add the run to the "Latest by
   family" and "All runs" tables in `docs/performance/index.md` (family:
   `tap-compare`) and check `uv run mkdocs build --strict`.
3. **Pre-registration tag**: before the first run whose numbers will be
   quoted publicly, tag the commit holding the protocol
   (`git tag tap-compare-prereg-v1 && git push --tags`) so the query
   classes, mix, windows and tie rule are provably frozen before the data
   existed. Optionally offer the DaCHS maintainers a look at
   `targets/dachs/` ("correct our deployment") and record the exchange.
4. **Phase 3 — CADC TAP target** (after a working DaCHS comparison): compose
   file + TAP_SCHEMA/obscore init SQL with pgsphere `spoint` columns per
   opencadc docs (Rubin's `lsst-sqre/cadc-tap-postgres` chart is the config
   reference); probe cone-search geometry support before anything else —
   the agreement gate catches wrong answers, not missing capability.
5. **Phase 5 — depth**: async/UWS rung (the runner's `_issue_async` is
   ready), D2 10 GiB tier repeat, egernia-only parquet/arrow appendix,
   open-loop fixed-rate latency rungs at {25,50,70,90}% of each server's
   measured capacity (retires the coordinated-omission critique).

## Claims policy

Results may claim the relative behaviour *of the measured versions, on this
hardware, on this corpus, as deployed by their own documentation, under the
recorded resource pins* — with confidence intervals, per query class, ties
reported as ties (overlapping 95% CIs, or under 10% apart in throughput).
A target whose requests errored beyond 1% cannot win a cell (its error
responses inflate its throughput; the error rate is printed beside the
number); a tripped generator guard, or both targets erroring, voids the
cell's verdict.
They may not claim anything about other corpus sizes, other hardware, other
versions, or classes a gate excluded. Classes where egernia loses are
reported with the same prominence as classes where it wins.
