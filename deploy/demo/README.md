# The multi-machine demo

The service on a Kubernetes cluster you already have, a hundred gigabytes of
metadata in it, and a notebook driving it from someone else's laptop.

This is deliberately not the single-machine walkthrough — that is
[`demo/srcnet_metadata_tap.ipynb`](../../demo/README.md), which runs against
`docker compose` and tours the data models. This one exists to answer a
different question: *does it hold up, and does it scale, when the client is
somewhere else?*

## The shape of it

Two machines and one SSH tunnel:

```
laptop                              cluster host
------                              ------------
notebook                            ingress controller
  |                                       |
  http://egernia.test:8080  ──ssh -L──>  :80 ──> /tap
                                              ──> /api/v1
  /etc/hosts: 127.0.0.1 egernia.test          ──> /prometheus
```

One forwarded port carries everything, because the ingress routes by path.
It routes by **Host** as well, which is what the `/etc/hosts` line is for:
the name has to be in the request, not just in the address bar. `.test` is
reserved by RFC 6761 for exactly this and can never collide with a real
domain.

The `/etc/hosts` line is a convenience, not a prerequisite. The deployment
also answers to `localhost` and to any name at all, and every URL it prints
back — job locations, result links, the capabilities `accessURL` — names the
host you actually reached it by. So `http://localhost:8080` works end to end,
async jobs included, with nothing added to `/etc/hosts`; use the name if you
want the demo to look like a real deployment.

## On the cluster

A StorageClass that can give it ~200 GiB, an ingress controller, and
metrics-server. `make demo-preflight` checks all three and says which is
missing rather than letting you find out from a `Pending` PVC twenty minutes
later.

```bash
kubectl config use-context <your-cluster>
make demo-preflight
make demo-deploy INGRESS_CLASS=nginx STORAGE_CLASS=fast-ssd
```

The host defaults to `egernia.test`; override with `DEMO_HOST=`. The cluster
is whatever `kubectl config current-context` points at, and every target that
changes it prints that context first — deploying 100 GiB into the wrong
cluster is not an undo-able mistake.

## On the laptop

```bash
echo '127.0.0.1 egernia.test' | sudo tee -a /etc/hosts   # optional, once
make demo-tunnel HOST=user@cluster-host
```

`demo-tunnel` finds where the ingress controller is reachable from the
cluster host — a NodePort, a LoadBalancer address, or neither — prints the
exact `ssh -L` line, and offers to run it. Leave it open; everything is then
at `http://egernia.test:8080`.

### TLS, if you want the padlock

```bash
make demo-tls                      # self-signed cert for egernia.test
make demo-deploy TLS_SECRET=egernia-demo-tls
make demo-notebook BASE_URL=https://egernia.test:8443 INSECURE_TLS=1
```

Worth being straight about what this buys: the tunnel is already an
encrypted, authenticated channel, so a certificate inside it protects nothing
the tunnel does not already protect — it costs a trust decision on every
laptop instead. It is here because "why is it not https" is a fair question
to be asked in front of an audience, and a working padlock answers it faster
than that paragraph does.

`INSECURE_TLS=1` makes the notebook skip verification, which is defensible
only because of the tunnel underneath. PyVO uses its own HTTP stack and will
not honour it, so the PyVO cell needs the certificate genuinely trusted —
another reason the default is plain HTTP.

### No ingress controller?

Fall back to NodePorts and two forwards. There is no Host routing that way,
so Prometheus needs its own port:

```bash
helm upgrade ... --set ingress.enabled=false \
    --set tapApi.service.type=NodePort --set tapApi.service.nodePort=30080 \
    --set prometheus.service.type=NodePort --set prometheus.service.nodePort=30090
ssh -N -L 8080:localhost:30080 -L 9090:localhost:30090 user@cluster-host
make demo-notebook BASE_URL=http://localhost:8080 \
                   EGERNIA_PROMETHEUS_URL=http://localhost:9090
```

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

With the tunnel open:

```bash
uv sync --group demo
make demo-notebook
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
