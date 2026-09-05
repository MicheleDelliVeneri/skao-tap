# The resource-scaling comparison: pre-registered protocol

Frozen before any measurement at tag `tap-compare-scaling-prereg-v1`. It
extends the parity protocol (`config/`, tag `tap-compare-prereg-v1`), whose
corpus, query classes, mix, formats, gates, statistics and tie rule it
reuses unchanged; only what is stated here differs.

## The question

The parity run (`docs/performance/20260903T160103Z-8fec0e75-tap-compare/`)
holds egernia and GAVO DaCHS to 8 CPUs / 8 GiB and one process each;
egernia won 113 of 120 cells. This experiment gives **both** servers twice
and then three times those resources and measures what each does with
them. The architectural expectation, stated in advance:

- **DaCHS** is one Python process (`dachs serve`) over its own
  PostgreSQL. Its throughput
  was flat at ~10 requests/s from 4 to 32 clients in the parity run; more
  cores and memory should leave it flat (**H1**: for every class and
  format, DaCHS's tier-16 and tier-24 throughput is a *tie* with its tier-8
  throughput by the pre-registered tie rule).
- **egernia** is a stateless API whose capacity is its worker count (each
  worker ≈ one core: `docs/deployment.md`, "Serving concurrent queries"),
  over a PostgreSQL whose parallel budget and buffer cache are sized to the
  container (`docs/postgres-performance.md`, "Sizing the server to its
  container", PR #146). Its throughput should grow with the tier (**H2**:
  egernia's mixed-workload throughput at c=64 at tier 16 and at tier 24
  exceeds its tier-8 value with non-overlapping 95% intervals).
- **Sanity (H3)**: egernia's tier-8 cells reproduce the parity run's c=8
  cells within their 95% intervals — the tier-8 egernia shape *is* the
  parity shape (asserted by `tests/test_scaling.py`).

Any hypothesis that fails is reported with the same prominence as one that
holds.

## Tiers and the host's limit

| tier | per server | egernia cpuset | DaCHS cpuset + `cpus` | generator cores |
| ---: | --- | --- | --- | --- |
| 8 | 8 CPUs, 8 GiB | 0–7 | 0–7, 8 | 24–29 |
| 16 | 16 CPUs, 16 GiB | 0–15 | 0–15, 16 | 24–29 |
| 24 | 24 CPUs, 24 GiB | 0–23 | 0–23, 24 | 24–29 |

The request was 8 → 16 → 32. The benchmark host has 30 vCPUs and 120 GiB:
32 cores cannot be pinned, and 24 is the ceiling that leaves six cores for
the load generator. A 32 tier is one more pins pair (`pins/egernia-32.yml`,
`pins/dachs-32.yml`, `pins/dachs-postgres-32.conf`, built by the same rules
below) plus `TIERS="8 16 32"` on a host with at least 40 cores; nothing
else changes.

**Consequence — sequential measurement.** Two 16- or 24-core stacks cannot
hold disjoint cpusets on this host, so within a tier the servers are
measured **one at a time**: both stacks come up under the tier's pins for
the gates (functional checks, untimed), then one is stopped while the other
is measured, then the roles swap. Repetitions therefore do not interleave
across servers as they did in the parity protocol (A,B,A,B): host drift
over a server's ~5 h block lands on that server. Mitigations: the order
alternates per tier (tier 8: egernia then DaCHS; tier 16: DaCHS then
egernia; tier 24: egernia then DaCHS), the three repetitions of a cell are
still spread over the block (the grid loops classes and concurrencies
outside repetitions), and nothing else runs on the pinned cores. This is
listed under threats to validity in the published report.

## Per-tier shapes

### egernia (`pins/egernia-<tier>.yml`)

The three serving containers share one cpuset of the tier's size (like
DaCHS's single pool of `cpus`), and the tier's memory splits 1/2 database,
1/4 API, 1/4 executor — the parity split (4/2/2 of 8 GiB), scaled.

| tier | db | api | executor | API workers | `shared_buffers` | `effective_cache_size` | `max_parallel_workers` | `max_worker_processes` |
| ---: | --- | --- | --- | ---: | --- | --- | ---: | ---: |
| 8 | 4g | 2g | 2g | 1 | 1GB | 3GB | 32 | 40 |
| 16 | 8g | 4g | 4g | 2 | 2GB | 6GB | 48 | 56 |
| 24 | 12g | 6g | 6g | 4 | 3GB | 9GB | 80 | 88 |

- **API workers 1 → 2 → 4** (`TAP_API_WORKERS`). The documented rule is one
  worker per core the API may use; one worker saturates at ~1 core because
  half of a request's CPU holds the GIL. The parity shape gives the API one
  of eight shared cores; the tiers give it two of sixteen and four of
  twenty-four, keeping the database — where VOTable rendering (PR #145) and
  the heavy classes' scans run — the large majority of the cpuset. Workers
  rather than compose replicas: the two axes buy the same throughput per
  process (`docs/deployment.md`: 335.8 vs 342.9 req/s for 2×2 vs 4×1), a
  worker costs no extra container, and the compose stack publishes one host
  port. Memory floor per the same page: ~140 MiB × workers + workers × 8 ×
  2.5 MiB — 660 MiB at four workers, well inside 6g.
- **Pool size unchanged** (8 per process). The connection budget is
  (workers + 1 executor) × 8 = 16 / 24 / 40, under `max_connections` 100.
- **PostgreSQL by PR #146's rule** for the db container's limit and the
  pool total: `shared_buffers` = 1/4, `effective_cache_size` = 3/4,
  `max_parallel_workers` ≥ pool total × 2 (per-gather default), 
  `max_worker_processes` = that + 8; `work_mem` stays 64MB. Tier 8 equals
  `docker-compose.yml` exactly.
- The executor is idle in this grid (sync rungs only); it keeps its share
  so the stack's shape stays the deployable one.

### DaCHS (`pins/dachs-<tier>.yml`, `pins/dachs-postgres-<tier>.conf`)

`docker-compose.dachs.yml` with `cpus` and `mem_limit` raised to the tier,
plus two deliberate additions, both applied at every tier including 8:

- **A cpuset** matching the tier, so DaCHS stays off the generator cores.
  The parity run pinned DaCHS by CFS quota alone (floating over all 30
  cores). Same core budget, different placement.
- **Its PostgreSQL sized to the container by the same rule.** The Debian
  package leaves `postgresql.conf` at 128MB `shared_buffers` and 4GB
  `effective_cache_size` whatever the container's memory; DaCHS's own
  documentation (install guide and docs index, checked 2026-09-05) says
  nothing about PostgreSQL tuning, and DaCHS exposes no memory tunable of
  its own. Left stock, tiers 16 and 24 would measure a stale default rather
  than the server. A `conf.d` drop-in (Debian's standard mechanism) sets
  `shared_buffers` to 1/4 and `effective_cache_size` to 3/4 of the
  container's limit: 2GB/6GB, 4GB/12GB, 6GB/18GB — and gives its
  PostgreSQL the same parallel budget egernia's database gets at the tier
  (`max_parallel_workers` 32/48/80, `max_worker_processes` 40/56/88).
  DaCHS's `[db] poolSize = 2` is per pool, not a ceiling: under eight
  clients it held eleven connections, eight of them active, so the Debian
  default of 8 parallel workers would starve its parallel plans exactly as
  PR #146 found for egernia. `poolSize` itself is not touched: raising it
  would be tuning DaCHS beyond its documentation, which the fairness rules
  forbid.

Because of these two additions DaCHS's tier-8 cells are a re-measurement
under a fairer PostgreSQL, not a replication of the parity run; the
difference between them is reported as what the default cost DaCHS.

## Workload, gates, statistics — unchanged

Corpus (seed 424242, 400 combinations, 3906 projects, portable ObsCore
classes Q01–Q07, Q10–Q13 + the mix), `MAXREC=100000` on every request,
VOTable and CSV, 3 repetitions, mean ± 95% Student-t interval, the tie
rule (overlapping intervals or under 10% apart), the 1% error ceiling, the
60% single-core generator guard: all as in `config/scenarios.yaml`
(`tests/test_scaling.py` asserts the shared blocks are identical). Gates —
VOSI capture, `stilts taplint`, and the cross-server agreement check — run
**once per tier** with both stacks up, and are recorded per tier
(`t<tier>-gates.json`); a taplint failure refuses that tier.

Generator: six processes (`generator_processes: 6`) pinned with `taskset`
to cores 24–29, which no tier's servers use. Six rather than the parity
protocol's four because the guard must hold at the largest tier's c=64
throughput; at c=8 the held concurrency shards 2,2,1,1,1,1 across them
(each shard draws its own seeded query stream, so the union is the same
workload as before, differently split).

An untimed 45 s mixed-workload pass (`warm`) precedes each server's
measured block, so the first repetition does not pay for a cold
`shared_buffers` after a container (re)start; its results directory is a
discard.

## The grid and its wall-clock

Per server per tier (`scaling` in `scenarios.yaml`):

    formats 2 × classes 12 (11 + mix) × concurrency {8, 32, 64} × 3 repetitions
      = 216 rungs × (20 s warm-up + 60 s window + ~3 s overhead) ≈ 216 × 83 s ≈ 4.98 h

The ~3 s overhead is the parity run's: 721 rungs in 30.6 h = 2.55 min each
for a 150 s rung. Six server-tier blocks ≈ 29.9 h; per tier add the gates
(~1.5 min), two warm passes (1.5 min) and four stack transitions (~4 min)
≈ 0.4 h in total. **Expected ≈ 30.3 h**, inside the 36 h budget with ~5.7 h
for overruns (a rung whose last streaming response drips past the window;
DaCHS Q11 at c=64 will have a p95 near 25 s).

Why this grid: the parity grid (360 rungs × 2.55 min = 15.3 h per server)
would need 92 h for six blocks. Concurrency 8 anchors the sanity check
against the parity run; 64 is above every parity knee (DaCHS flat from 4,
egernia's peak at 4–8 with a single worker) and is where a server with
more workers and connections can show it; 32 shows the shape between them.
Windows of 60 s hold ≥ 600 requests per rung for DaCHS's slowest class at
c=8 and thousands for egernia; the 20 s warm-up follows the 45 s warm pass
and the per-rung request stream itself. `[8, 64]` (≈ 20 h) was the fallback
if the budget had been tighter.

## Procedure (`run.sh`)

For each tier, in order 8, 16, 24:

1. `up egernia`, `up dachs` under the tier's pins (`docker compose up -d
   --no-build` with the pins override recreates the containers on their
   existing volumes; nothing is re-seeded or re-ingested). After each `up`:
   `ivoa.obscore` row count = 500,096 on both, the trigram index and the 16
   `srcnet` foreign keys present on egernia, `SHOW` of every PostgreSQL
   setting the pins promise, and the API's process count for its worker
   count. A mismatch aborts.
2. Gates with both up (`compare --tier <tier> --gates-only`), then the pins
   as applied — `docker inspect` cpuset/quota/memory, container command
   lines, `docker top`, `SHOW` — into `pins/t<tier>-<target>.{json,txt}` in
   the run directory.
3. Stop the second server; warm and measure the first
   (`compare --tier <tier> --only <first>`); stop it.
4. Start the second (re-verified and re-recorded); warm and measure it;
   stop it.

Everything lands in one run directory
(`results/<stamp>-<sha>-tap-compare-scaling`), resumable with
`RUN_NAME=<dir> TIERS="<remaining>" run.sh`. At the end both stacks return
to the parity pins. `publish --run <dir>` renders one section per tier,
each with its gates and its VOTable and CSV tables, plus the `pins/`
records.

## Claims policy

Per tier, exactly the parity policy: relative behaviour of these versions,
on this hardware, on this corpus, as deployed by their own documentation,
under the recorded pins; ties as ties; a target erroring beyond 1% cannot
win a cell; a tripped generator guard voids the cell. Across tiers, the
claim is **per server**: how a server's throughput and p95 in a cell change
from tier 8 to 16 to 24, judged by the same tie rule between tiers. The
report may not claim anything about other corpus sizes, other hosts, a 32
tier this host could not run, DaCHS under a configuration its documentation
does not describe, or classes a gate excluded.

## Threats to validity (in addition to the parity protocol's)

- Sequential measurement within a tier (above); order alternated, recorded.
- The second server in each tier is restarted between the gates and its
  block; the warm pass and per-rung warm-up mitigate a cold cache.
- DaCHS's tier-8 shape differs from the parity run's (cpuset, PostgreSQL
  drop-in); only egernia's tier-8 cells are a replication.
- DaCHS's ceiling is a design choice (one Python process) the protocol
  does not alter; a DaCHS operator might tune it differently.
- Host NUMA/L3 topology is not controlled; larger cpusets may span more of
  it. Idle neighbours (a Prometheus, stopped leftovers, the toolkit
  containers) share the host's memory bandwidth, not the pinned cores.
- Six generator processes instead of four change how the held concurrency
  is sharded, not the workload drawn.

## Deviations from this document

None are allowed silently. Anything that has to change once measurement has
started is recorded in the run's `environment.json` (`resumed` entries
carry the git state) and in the published report's run notes.
