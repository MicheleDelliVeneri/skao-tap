# Autoscaling

Both services scale out, and they scale on different things — because they
are limited by different things.

| | Signal | Needs |
| --- | --- | --- |
| tap-api | CPU utilisation | metrics-server, which every cluster runs |
| tap-executor | `tap_oldest_queued_job_seconds` | KEDA, or an external-metrics provider |

Off by default. A single-replica deployment is a valid one, and handing the
replica count to an autoscaler is a decision an operator should make rather
than inherit.

## The API scales on CPU

ADQL translation is pure-Python ANTLR and holds the GIL, so one worker cannot
use more than one core however large the pod's CPU limit is — which makes CPU
the signal, and it needs nothing beyond metrics-server:

!!! note "The worker figures here predate the translation fast path"
    They were measured when translation cost 41 ms per request; it now costs
    1.2 ms. More workers still add capacity, but the numbers below understate
    what one worker does and are being re-measured — see
    [Performance](performance/index.md).

```yaml
horizontalAutoscaling:
  tapApi:
    enabled: true
    minReplicas: 2
    maxReplicas: 6
    targetCPUUtilizationPercentage: 70
```

The target is a percentage of the pod's CPU *request*, so
`tapApi.resources.requests.cpu` has to be set — the chart refuses to render
an HPA without it, because a utilisation target with no request is a metric
Kubernetes reports as unavailable, and an autoscaler that never acts is worse
than none.

**Do not scale the API on database pool waits.** It is tempting —
`tap_db_pool_wait_seconds` is right there, and it rises under load. But a
pool wait means the *database* is the limit, and every new API pod opens its
own pool: scaling on it adds connections to a server that already has none to
give. That is the one signal on the dashboard that must not become a scaling
rule.

### Alongside a VPA: one controller per resource

Package 5 ships optional VerticalPodAutoscalers, and a VPA in `Auto` mode
that sizes CPU resizes the pod while the HPA counts pods against a percentage
of the size the VPA just changed. Kubernetes documents that as unsupported,
so the chart refuses it — but only that: two controllers driving *the same*
resource is the problem, not the pair. Either keep the VPA in
recommendation mode, which stays useful (`kubectl describe vpa`) while the
HPA scales:

```yaml
verticalAutoscaling: {enabled: true, updateMode: "Off"}
```

or split the resources, which is supported and often what you want — the VPA
sizes memory, where a right-sized limit avoids OOM kills, and the HPA counts
pods on CPU:

```yaml
verticalAutoscaling:
  enabled: true
  updateMode: Auto
  controlledResources: ["memory"]
```

## The executor scales on the queue

An executor spends its time waiting on PostgreSQL, so its CPU says nothing
about whether it is keeping up. What a user waits for is the queue, and
package 8 exports exactly that: `tap_oldest_queued_job_seconds`, the age of
the oldest `QUEUED` job.

The knob is seconds of backlog one replica should absorb, and the autoscaler
divides:

```
replicas = ceil(backlog_seconds / backlogSecondsPerReplica)
```

So 300 s of backlog against the default 60 asks for 5 executors. Lower it to
react sooner and run more; raise it to tolerate a queue.

### With KEDA (default)

```yaml
horizontalAutoscaling:
  tapExecutor:
    enabled: true
    maxReplicas: 8
    backlogSecondsPerReplica: 60
    prometheusAddress: http://prometheus-operated.monitoring:9090
```

KEDA is Apache-2.0 and a graduated CNCF project; install the operator, and
the chart's `ScaledObject` carries the PromQL with it. That is the reason
this is the default: the query is reviewed here, next to the metric it reads,
rather than in another release's values.

The query it ships is

```promql
max(tap_oldest_queued_job_seconds{namespace="<release namespace>"})
```

`max()`, not `sum()`. Every replica reports the same figures for one shared
queue, so `sum()` would scale on the replica count — more executors, bigger
sum, more executors, which is an autoscaler feeding on its own output. The
namespace selector keeps a Prometheus that watches several namespaces from
scaling this release on another one's backlog. Override `query` if your
scraper adds no `namespace` label, or if two releases share one namespace.
(`query` reaches KEDA only — the `external` scaler below has its own knob for
the same job.)

### Without KEDA

If the cluster already serves external metrics — `prometheus-adapter` is the
usual one, also Apache-2.0, from kubernetes-sigs — a plain HPA can read the
same gauge:

```yaml
horizontalAutoscaling:
  tapExecutor:
    enabled: true
    scaler: external
```

The chart then renders a `HorizontalPodAutoscaler` with an `External` metric
and records the query it expects in an annotation, because the query itself
now lives in the adapter's configuration, not here. The adapter rule has to
aggregate the same way:

```yaml
# prometheus-adapter values
rules:
  external:
    - seriesQuery: 'tap_oldest_queued_job_seconds'
      resources:
        overrides:
          namespace: {resource: namespace}
      name:
        as: tap_oldest_queued_job_seconds
      metricsQuery: max(<<.Series>>{<<.LabelMatchers>>})
```

`max(...)` there for the same reason as above. Getting it wrong in that file
produces an autoscaler that looks healthy and scales on nonsense, which is
the trade this option makes: one less component to install, one more place
where the query can be wrong.

The metric is scoped by a label selector, not by the `query` value — that one
only reaches KEDA. By default the chart selects `namespace: <release
namespace>`, which is what the rule above publishes. A provider that exposes
different labels, or none, needs the selector changed to match, because a
selector matching no series does not make the autoscaler stricter — it
leaves it at `<unknown>` for good:

```yaml
horizontalAutoscaling:
  tapExecutor:
    externalMetricSelector: {}                    # no labels published
    # externalMetricSelector: {job: tap-executor} # or different ones
```

### Scaling to zero, and why the default query cannot

The default query reads a gauge the executor itself exports. With no
executors running nothing reports the queue, the series goes stale a few
minutes later, and no amount of queued work can bring a pod back — a
deadlock, not an optimisation. So the chart refuses `minReplicas: 0` while
the query is the default one.

It is not refused in general, because the deadlock is a property of *that*
series rather than of scaling to zero. Point `query` at something that
survives having no executors — a recording rule, or an exporter reading
`uws.jobs` directly — and zero is sound:

```yaml
horizontalAutoscaling:
  tapExecutor:
    minReplicas: 0
    query: max(tap_queue_depth_from_db)   # not exported by the executor
    activationBacklogSeconds: 5           # backlog worth starting a pod for
```

`activationBacklogSeconds` governs the 0→1 transition only, so the chart
renders it only when the minimum is 0; at a minimum of 1 it would be a
setting that looks live and does nothing. A plain HPA (`scaler: external`)
cannot reach zero at all without the `HPAScaleToZero` feature gate, and is
refused with a message saying so.

## The connection ceiling moves with the replica count

Every API worker and every executor holds its own pool, so the peak number
of database connections is

```
tapApi.maxReplicas x tapApi.workers x config.dbPoolMax
  + tapExecutor.maxReplicas x config.dbPoolMax
```

Autoscaling makes that number reachable without anyone deciding to reach it.
When `postgresql.tuning.max_connections` says what the limit is, the chart
does the arithmetic and refuses a configuration that would scale into a
connection wall — PostgreSQL holds 3 connections back for superusers, so
that is subtracted:

```console
$ helm template … --set horizontalAutoscaling.tapApi.enabled=true \
    --set tapApi.workers=4 --set postgresql.tuning.max_connections=100
Error: … fully scaled out, this release would open 200 database connections
(API: 6 pods x 4 workers x dbPoolMax 8; executor: 1 x 8), but
postgresql.tuning.max_connections is 100 …
```

Raise `max_connections`, lower `config.dbPoolMax`, or lower the maximums. For
a deployment that genuinely needs many pods against a small server, put
pgbouncer in front — the pool ceiling is then the pooler's, not the sum of
every pod's.

## Damping

Both paths accept an `autoscaling/v2` `behavior:` block, passed through
verbatim, for sites that want scale-down slower than the 5-minute default:

```yaml
horizontalAutoscaling:
  tapExecutor:
    behavior:
      scaleDown:
        stabilizationWindowSeconds: 600
```

That is where hysteresis belongs for the scaling range.
`activationBacklogSeconds` is a different thing and covers only the 0→1
transition, which is why it is rendered only when the minimum is 0.

## Checking it works

```console
$ kubectl get hpa
NAME           REFERENCE              TARGETS   MINPODS  MAXPODS  REPLICAS
tap-api        Deployment/tap-api     12%/70%   2        6        2
$ kubectl get scaledobject          # with KEDA
$ kubectl describe scaledobject tap-executor   # trigger errors show here
```

An HPA reporting `<unknown>` for its target is the usual symptom: for the API
that means no CPU request or no metrics-server; for the executor, that the
metrics provider is not serving the metric — check the Prometheus named in
`prometheusAddress` actually has `tap_oldest_queued_job_seconds`, which needs
the executor pods being scraped in the first place (see
[Observability](observability.md)).

A fresh install starts at one pod per autoscaled service, because the chart
no longer sends a replica count and a Deployment defaults to one; the
autoscaler raises it to `minReplicas` on its first reconcile, within about
fifteen seconds. `helm install --wait` returns before that, so a pod count
read immediately after an install is not the steady state.

While an autoscaler owns a Deployment, the chart stops rendering `replicas`
for it. A `helm upgrade` that set it would reset the count the autoscaler had
chosen, and the two would then take turns overwriting each other.
`podDisruptionBudget` follows `minReplicas` instead, since that — not the
static value the autoscaler ignores — is the floor a node drain has to
respect.
