# The multi-machine demo

The service on a Kubernetes cluster you already have, a hundred gigabytes of
metadata in it, and a notebook driving it from someone else's laptop.

This is deliberately not the single-machine walkthrough — that is
[`demo/srcnet_metadata_tap.ipynb`](../../demo/README.md), which runs against
`docker compose` and tours the data models. This one exists to answer a
different question: *does it hold up, and does it scale, when the client is
somewhere else?*

## What you need

A cluster with a StorageClass that can give it ~200 GiB, an ingress
controller, and metrics-server. `make demo-preflight` checks all three and
says which is missing rather than letting you find out from a `Pending` PVC
twenty minutes later.

```bash
kubectl config use-context <your-cluster>
make demo-preflight
```

## Deploying

```bash
make demo-deploy HOST=tap.example.org \
                 INGRESS_CLASS=nginx \
                 STORAGE_CLASS=fast-ssd
```

`HOST` is required and must be a name the **notebook machine** can resolve to
the ingress address — DNS, or a line in its `/etc/hosts`. `make demo-status`
prints both the hostname and the address so you can check they agree.

The cluster is whatever `kubectl config current-context` points at. Every
target that changes it prints that context first: deploying 100 GiB into the
wrong cluster is not an undo-able mistake.

## The dataset

```bash
make demo-dataset      # hours, once
make demo-snapshot     # so it is never hours again
```

Generation grows one database through D1 → D5, checkpointing at each tier, and
is resumable: a run that dies at 60 GiB restarts near 60, not at zero. Every
row crosses a port-forward, so run it from a machine with a fast path to the
cluster — not from the laptop that will present over hotel wifi.

`demo-snapshot` takes a `VolumeSnapshot`, which needs a CSI driver that
supports them. Without one, keep the data between demos instead:

```bash
make demo-teardown KEEP_DATA=1
```

D5 is defined in `benchmarks/egernia-performance/config/datasets.yaml`
alongside the benchmark tiers but marked `demo_only`, so the families that
sweep every tier do not pick it up — a 55 GiB addition to every `db-scaling`
run would buy a curve that D1–D4 already gives the shape of.

## The notebook

```bash
uv sync --group demo
make demo-notebook HOST=tap.example.org
```

marimo, so the sliders are live: change the query, the cone radius or the
concurrency and only the cells that depend on them re-run. Four parts:

**1. What a client can discover.** Every discovery endpoint answered live —
the service root, OpenAPI and Swagger, both health probes, VOSI availability,
capabilities and tables, DALI examples, the VOResource record and the metrics
exposition — with what each is for. Then the capabilities document reduced to
what a client actually branches on (ADQL versions, output formats, upload
methods, declared data models), and `/api/v1/auth`, which says what the
deployment enforces so nobody has to discover it by trial.

**2. Four ways to ask the same question.** One ADQL box drives all of them:

- **PyVO** (`pyvo.dal.TAPService`) — the client an astronomer already has,
  parsing the VOTable's units and UCDs knowing only a URL
- **raw TAP** (`POST /tap/sync`) — what PyVO does underneath, available to any
  language with an HTTP client
- **the JSON API** (`POST /api/v1/query`) — no XML, no VO library, and column
  metadata in the response so a caller still knows `s_ra` is degrees
- **asynchronous** — the same query as a UWS job *and* as a JSON job, ending
  by showing the JSON job's own `urls.uws`: one job store, two protocols

Plus how it refuses. Three bad requests, each answered as a *usage* error
rather than a 500 — including `INTERSECTS(s_region, ...)`, where the service
names `s_region_geom` instead of letting PostgreSQL fail with
`operator does not exist: text && scircle`.

**3. Two metadata models, one query language.** A join up the ODP hierarchy
and a query over the software records — different pydantic models, flattened
by the same generator into `srcnet.*`, registered in the same `TAP_SCHEMA`,
neither with a line of hand-written schema.

**4. At scale.** A spatial query over a hundred gigabytes answering in
milliseconds because the footprint is GiST-indexed; a full-table aggregate
taking **seconds**, on purpose, because a demo that only shows the fast case
teaches the wrong lesson; thousands of requests at a chosen concurrency with
the latency distribution; and what the cluster did about it — pods up, request
rate, per-pod CPU — from the Prometheus the chart deploys.

## Headlamp

```bash
make demo-headlamp
kubectl port-forward -n egernia-demo service/headlamp 8081:80
```

Bound to the built-in read-only `view` ClusterRole: an audience watching pods
appear should see the cluster, not be able to change it. It is reached by
port-forward rather than an ingress because Headlamp authenticates by bearer
token, and publishing that is a decision the script will not take for you.

## Teardown

```bash
make demo-teardown              # release and namespace, dataset included
make demo-teardown KEEP_DATA=1  # keep the PVC for next time
```

## What this demo will not tell you

The numbers on screen are one session on one cluster, without repetitions or
intervals. They are an illustration, not a measurement. The reproducible
figures — with confidence intervals, provenance, and a bottleneck
classification saying *which* resource ran out — come from the benchmark
suite and are published under [`docs/performance`](../../docs/performance).

If a query is slower than the notebook's prose suggests, the first suspect is
a cold cache: the tiers above 25 GiB have a working set larger than memory,
which is why the benchmark config gives them warmups of five and ten minutes.
