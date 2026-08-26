# Deployment

## Docker Compose (development)

```bash
docker compose up --build -d
./scripts/smoke_test.sh
```

Compose builds three images: `db` (PostgreSQL 18 + pg_sphere, initialized
from `db/init/*.sql`), `tap-api` and `tap-executor` (both installed with
`uv sync --frozen` from the committed `uv.lock`).

## Helm (Kubernetes)

A chart is provided under `deploy/helm/egernia`:

```bash
helm upgrade --install egernia deploy/helm/egernia \
  --namespace egernia --create-namespace \
  --set tapApi.baseUrl=https://tap.example.org/tap
helm test egernia -n egernia
```

!!! warning "Upgrading an install made before the rename"

    The project was called `skao-tap`, and every object the chart creates is
    named `<release>-<component>`. A release installed as `skao-tap` therefore
    cannot be renamed in place — `helm upgrade egernia` would create a second
    set of objects rather than adopt the first. Uninstall and reinstall:

    ```bash
    helm uninstall skao-tap -n skao-tap     # keeps PVCs unless they are deleted
    helm upgrade --install egernia deploy/helm/egernia -n egernia --create-namespace
    ```

    The database and results PersistentVolumeClaims are named after the
    release as well (`skao-tap-db`, `skao-tap-results`), so data that has to
    survive must be moved: back up with the chart's own dump path (below),
    reinstall, and restore into `egernia-db`.

Key values (see `values.yaml` for the full list):

| Value | Default | Description |
|---|---|---|
| `image.registry` / `image.tag` | `ghcr.io/ska-telescope` / chart appVersion | Where CI publishes the service images |
| `tapApi.replicas` | `1` | API replicas (stateless) |
| `tapApi.baseUrl` | in-cluster service URL | External base URL written into capabilities and result links |
| `tapExecutor.replicas` | `1` | Executor replicas; safe to scale out (jobs claimed with `SKIP LOCKED`) |
| `postgresql.enabled` | `true` | Deploy the in-chart PostgreSQL + pg_sphere; disable to use an external DB via `externalDatabase.url` |
| `results.storageClass` / `results.size` | `""` / `1Gi` | Shared results volume |
| `ingress.enabled` | `false` | Optional ingress for the API |
| `scheduling.spreadReplicas` | `true` | Soft anti-affinity + zone spread for multi-replica services |
| `podDisruptionBudget.enabled` | `true` | PDBs for components with >1 replica |
| `verticalAutoscaling.enabled` | `false` | VPA per service (recommendation mode first) |
| `postgresql.tuning` | `{}` | postgresql.conf overrides as `-c` server arguments |
| `backup.enabled` | `false` | Nightly `pg_dump` CronJob to a dedicated PVC |
| `metrics.scrapeAnnotations` | `true` | Annotate pods so an existing Prometheus discovers them ([guide](observability.md)) |
| `prometheus.enabled` | `false` | Deploy a Prometheus for testing; production scrapes with its own |
| `tracing.otlpEndpoint` | `""` | Export traces; empty means no collector and no instrumentation |
| `config.executorMetricsPort` | `9100` | Port the executor serves metrics on — it has no API of its own |
| `config.dbPoolMax` | `8` | Database connections per process — the real limit on concurrent queries |
| `config.dbPoolTimeoutSeconds` | `5` | How long a request waits for one before answering `503` |
| `tapApi.workers` | `1` | Uvicorn processes per pod; ADQL translation holds the GIL, so this is what lets a pod use more than one core |
| `auth.enabled` | `false` | Require verified tokens and gate the mutating metadata endpoints ([guide](auth.md)) |
| `auth.requireToken` | `true` | With `auth.enabled`, every request needs a verified token — discovery and the health check aside |
| `auth.anonymousQueries` | `false` | Let token-less callers read metadata through `/tap/sync` and the `/tap/async` job; what standard VO clients need |
| `auth.gatedOperations` | `[]` | Which operations need an authorisation decision; empty means metadata mutation only |
| `auth.plugin` | `iam-groups` | Authorisation plugin: local IAM groups, or the SRCNet Permissions API |
| `auth.iam.issuer` | `""` | Required when `auth.enabled`; tokens are verified against its JWKS |
| `auth.iam.audience` | `""` | Required when `auth.enabled`; guards against cross-service token replay |
| `auth.roles` | `{}` | Per-operation groups/scopes for `iam-groups`; required, and an empty rule denies |

### Serving concurrent queries

Translating ADQL is pure-Python ANTLR work — tens of milliseconds per query,
holding the GIL — so a single process answers one query at a time no matter
how many cores the pod has. Measured locally at 8 concurrent clients, same
machine and same queries:

| workers | throughput | p95 |
| ---: | ---: | ---: |
| 1 | 59 req/s | 199 ms |
| 4 | 210 req/s | 61 ms |

!!! warning "Measured before the translation fast path"
    These were taken when ADQL translation cost 41 ms of a ~50 ms request. It
    now costs 1.2 ms, so a single worker goes very much further than this table
    suggests, and the ratio between one and four workers no longer holds.
    They are kept only until the re-measurement lands in
    [Performance](performance/index.md).

Set `tapApi.workers` to the pod's CPU limit and no higher: beyond that the
workers compete for the same cores and only latency moves. `tapApi.replicas`
does the same across pods, and the two combine.

### Shedding overload with refusals, not resets

Past its capacity the service currently sheds load with connection resets —
the benchmark suite observed clients reading `ECONNRESET` under sustained
overload, and a reset tells a client nothing: it cannot distinguish an
overloaded service from a broken network, so it can only retry blind, which
adds load. The leading suspect is the listen socket's accept queue
overflowing before the application ever sees the connection (unconfirmed —
the rate at which resets begin has not been established).

Two knobs shape this behaviour:

```yaml
tapApi:
  backlog: 2048          # accept-queue size (uvicorn --backlog)
  limitConcurrency: 0    # connections per worker before answering 503
```

`backlog` sizes the kernel's accept queue; raising it absorbs sharper
arrival bursts but adds queueing, not capacity. `limitConcurrency` is the
application-level counterpart: past that many concurrent connections *per
worker process*, uvicorn answers `503` immediately — a refusal a client can
back off from. It is off (`0`, unlimited) by default because the right value
depends on what one worker can actually serve; when set, put it above the
worker's normal concurrent load and below where resets were observed.

`/health/live` answers whether the process is turning and touches nothing else;
`/health/ready` answers whether this pod should be sent traffic, and treats a
full connection pool as *ready* — a busy service is not a broken one, and
taking the pod out of the Service would push its share of the load onto pods in
exactly the same state. Only an unreachable database makes it unready.

!!! warning "Do not point probes at `/tap/availability`"
    It is a VOSI resource that reports on the database, so it queues for a
    pooled connection. With the probe default of `timeoutSeconds: 1` it cannot
    answer under load, and the kubelet then restarts a service that is busy
    rather than broken — measured, twice, at an offered rate inside the
    service's own capacity. Both probes used to point there.

Both paths are exempt from authentication: a kubelet has no token and cannot
obtain one, so gating them would make enabling auth an outage.

### When every connection is busy

The pool bounds how many queries a process runs at once, and it is smaller
than the number of connections uvicorn accepts, so concurrency above it has
to queue. Measured locally with one worker and the default pool of 8:

| concurrent clients | throughput | p95 | errors |
| ---: | ---: | ---: | ---: |
| 8 | 66 req/s | 169 ms | 0 |
| 12 | 66 req/s | 238 ms | 0 |
| 16 | 66 req/s | 318 ms | 0 |

Throughput holds flat and latency grows: requests wait their turn, which is
what a queue should look like. Add `tapApi.workers` to raise the ceiling
itself — four workers carried 233 req/s at 32 clients on the same machine
(again, before the translation fast path).

If the wait for a connection exceeds `config.dbPoolTimeoutSeconds` the request
answers `503` with `Retry-After` rather than holding the caller. Five seconds
is deliberately short: a synchronous query may run for up to
`config.syncTimeoutSeconds`, so a queue of slow queries could otherwise leave
a client waiting a minute for a connection that a fast, retryable refusal
describes better.

Mind the connections. Each worker opens its own pool, so a pod holds up to
`tapApi.workers × config.dbPoolMax` connections, and the deployment holds that
multiplied by `tapApi.replicas` (or by `horizontalAutoscaling.tapApi.maxReplicas`
when an autoscaler is in charge — see [Autoscaling](autoscaling.md), which
checks this sum for you). With the defaults that is `1 × 8 = 8` per pod.
Keep the total under the server's `max_connections` —
`postgresql.tuning.max_connections` raises it for the in-chart database, and a
managed server has its own limit.

!!! warning "Results volume access mode"
    The results volume is shared between the API and the executor. With more
    than one node you need a `ReadWriteMany`-capable storage class (or pin
    both deployments to one node); the chart defaults to `ReadWriteOnce`
    which is only safe for single-node/dev clusters.

The in-chart PostgreSQL mounts the same `db/init` SQL (copied into the chart
at `files/db-init/`; CI verifies both copies stay in sync).

## Scaling and resilience

Both services are horizontally scalable: tap-api is stateless, and the
executors claim jobs with `FOR UPDATE SKIP LOCKED`, so extra replicas
cooperate instead of colliding. Give each service more than one replica and
switch the results volume to `ReadWriteMany`:

```bash
helm upgrade egernia deploy/helm/egernia \
  --set tapApi.replicas=3 \
  --set tapExecutor.replicas=2 \
  --set "results.accessModes={ReadWriteMany}" \
  --set results.storageClass=<an RWX-capable class>
```

With `scheduling.spreadReplicas` (on by default) the chart adds preferred
pod anti-affinity across nodes and a zone topology-spread constraint to each
service — soft constraints (`ScheduleAnyway`), so single-node clusters such
as kind schedule exactly as before. Per-component
`affinity`/`topologySpreadConstraints`/`nodeSelector`/`tolerations` values
override the defaults wholesale. Components with more than one replica also
get a PodDisruptionBudget (`maxUnavailable: 1`), keeping the service up
through node drains and cluster upgrades.

### Automatic scaling

Those replica counts can be handed to an autoscaler instead: tap-api on CPU,
tap-executor on the age of the oldest queued job. Both are off by default and
have their own page — [Autoscaling](autoscaling.md) — including the
combinations the chart refuses, among them the connection ceiling that a
maximum replica count makes reachable.

### Vertical scaling

`verticalAutoscaling.enabled=true` creates a VerticalPodAutoscaler per
service (requires the [VPA
CRDs](https://github.com/kubernetes/autoscaler/tree/master/vertical-pod-autoscaler)
in the cluster). It starts in recommendation mode — read the suggestions
with `kubectl describe vpa` — and moves to live resizing with
`verticalAutoscaling.updateMode=Auto` once the `minAllowed`/`maxAllowed`
bounds are trusted. `verticalAutoscaling.controlledResources` narrows what it
sizes — set it to `["memory"]` to run a live VPA next to a CPU-based
HorizontalPodAutoscaler, since two controllers driving the same resource is
what does not work (see [Autoscaling](autoscaling.md)).

The in-chart PostgreSQL is sized through `postgresql.resources` plus
`postgresql.tuning`, a map rendered as `-c key=value` server arguments:

```yaml
postgresql:
  resources:
    limits:
      memory: 2Gi
  tuning:
    max_connections: 100
    shared_buffers: 512MB
    effective_cache_size: 1536MB
    work_mem: 32MB          # per sort/hash node — large ADQL joins
    maintenance_work_mem: 128MB
```

Watch connection counts (each API/executor replica holds a pool),
`shared_buffers` hit rates, and temp-file spills from large ADQL sorts when
right-sizing.

### Highly available PostgreSQL

The in-chart StatefulSet is a single instance — fine for development and
small sites, not for HA. For automated failover run PostgreSQL under a
streaming-replication operator such as
[CloudNativePG](https://cloudnative-pg.io) (or Zalando's
postgres-operator), load `db/init/*.sql` into it once, and point the chart
at it:

```bash
helm upgrade egernia deploy/helm/egernia \
  --set postgresql.enabled=false \
  --set externalDatabase.url=postgresql://tap:…@tap-db-rw:5432/tap
```

The services only need the one DSN, so failover handled by the operator is
transparent to them. An operator-managed database also brings WAL archiving
and point-in-time recovery (below).

## Backup and restore

Two things hold state: the PostgreSQL database (UWS jobs, `TAP_SCHEMA`, all
ingested metadata) and the results volume (query outputs).

### Database

`backup.enabled=true` adds a CronJob that writes `pg_dump --format=custom`
archives of the whole database to a dedicated PVC and prunes them after
`backup.retentionDays`:

```bash
helm upgrade egernia deploy/helm/egernia \
  --set backup.enabled=true \
  --set backup.schedule="0 2 * * *" \
  --set backup.retentionDays=7 \
  --set backup.storage=10Gi
```

It dumps through `TAP_DATABASE_URL`, so it covers the in-chart PostgreSQL
and an external database alike. Keep `retentionDays` at or above
`config.jobRetentionSeconds` (default 7 days), or restored deployments will
be missing jobs their clients still consider alive.

To restore, stop the services so nothing writes during the restore, run
`pg_restore` from a pod with the backup PVC mounted, then scale back up:

```bash
kubectl scale deploy egernia-tap-api egernia-tap-executor --replicas=0

kubectl apply -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: pg-restore
spec:
  restartPolicy: Never
  containers:
    - name: pg-restore
      image: <the tap-db image>
      command: ["sh", "-ec"]
      args:
        # or name a specific archive instead of the most recent one
        - |
          archive=$(ls -t /backups/egernia-*.dump | head -1)
          echo "restoring ${archive}"
          pg_restore --clean --if-exists -d "$TAP_DATABASE_URL" "${archive}"
      env:
        - name: TAP_DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: egernia-db      # <release>-db
              key: TAP_DATABASE_URL
      volumeMounts:
        - name: backups
          mountPath: /backups
  volumes:
    - name: backups
      persistentVolumeClaim:
        claimName: egernia-db-backups   # <release>-db-backups
YAML

kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/pg-restore --timeout=10m
kubectl logs pg-restore && kubectl delete pod pg-restore
kubectl scale deploy egernia-tap-api --replicas=1
kubectl scale deploy egernia-tap-executor --replicas=1
```

Exercise the restore path regularly — an unrestored backup is a hope, not a
strategy. `pg_dump` gives consistent snapshots but no point-in-time
recovery; for PITR (base backups plus WAL archiving to object storage) run
the database under CloudNativePG and use its `Backup`/`ScheduledBackup`
resources, in which case `backup.enabled` here is redundant.

### Results volume

Job results are re-derivable (any job can be re-run) but re-deriving them
costs compute, and result URLs are handed to clients. Snapshot the results
PVC with the storage class's VolumeSnapshot support — or back it up to
object storage with a tool such as Velero — on a cadence and retention
aligned with `config.jobRetentionSeconds`: results older than the retention
window are destroyed anyway, so keeping their backups any longer buys
nothing. A restored results volume plus a database restored from the same
window keeps job documents and their result files consistent; results
missing for a restored job simply 404 and the job can be re-run.

## Container hardening

All three images run as non-root users (`tap`, uid 10001 for the services;
`postgres`, uid 999 for the database) and work with a read-only root
filesystem. The Helm chart sets matching pod/container security contexts
(`runAsNonRoot`, dropped capabilities, seccomp `RuntimeDefault`,
`readOnlyRootFilesystem`) with `emptyDir` mounts for `/tmp` and, for
PostgreSQL, `/var/run/postgresql`. The Trivy job in CI enforces this —
it fails on any CRITICAL/HIGH vulnerability, leaked secret, or
misconfiguration.

!!! note "Upgrading an existing Compose deployment"
    Volumes created by older root-based images keep root ownership; if the
    database or executor fails with permission errors after upgrading,
    recreate the volumes once with `docker compose down -v`.

!!! warning "PostgreSQL 18"
    The database image moved from PostgreSQL 16 to 18. A data directory
    written by 16 cannot be started by 18, and the `postgres:18` image also
    changed its default `PGDATA` (`/var/lib/postgresql/<major>/docker`, with
    `/var/lib/postgresql` as the declared volume) — Compose and the chart
    both pin `PGDATA` back under their mount. There is no production data to
    migrate, so recreate the volume: `docker compose down -v` for Compose, or
    delete the `data-<release>-postgres-0` PVC before upgrading the chart.
    Should a future major bump need to preserve data, take a
    `pg_dump --format=custom` archive beforehand and `pg_restore` it into the
    new cluster.

## Configuration

All services read environment variables (see `egernia_core/config.py`):

| Variable | Default | Description |
|---|---|---|
| `TAP_DATABASE_URL` | `postgresql://tap:tap@localhost:5432/tap` | PostgreSQL DSN |
| `TAP_BASE_URL` | `http://localhost:8080/tap` | Public base URL (capabilities, result links) |
| `TAP_RESULTS_DIR` | `/results` | Shared results directory |
| `TAP_QUERY_ROLE` | `tap_reader` | Read-only role used for user queries |
| `TAP_DEFAULT_MAXREC` / `TAP_HARD_MAXREC` | `10000` / `1000000` | Row limits |
| `TAP_SYNC_TIMEOUT` | `30` | Sync query timeout (s) |
| `TAP_ASYNC_EXEC_DURATION` | `600` | Default async `executionDuration` (s) |
| `TAP_JOB_RETENTION` | `604800` | Default job lifetime before destruction (s) |
| `TAP_MODEL_PLUGINS` | `all` | Metadata domains to activate (`all` or a comma-separated subset) |
| `TAP_AUTH_ENABLED` | `false` | Enable authentication/authorisation ([guide](auth.md)) |
| `TAP_AUTH_REQUIRE_TOKEN` | `true` | Require a verified token on every request bar discovery and the health check |
| `TAP_AUTH_ANONYMOUS_QUERIES` | `false` | Allow token-less reads through `/tap/sync` and `/tap/async` |
| `TAP_AUTH_GATED_OPERATIONS` | — | Operations needing an authorisation decision; empty means metadata mutation only, `none` means nothing |
| `TAP_AUTH_PLUGIN` | `iam-groups` | Which authorisation plugin decides (`iam-groups`, `permissions-api`, or your own) |
| `TAP_IAM_ISSUER` | — | Token issuer; tokens are always verified against its JWKS |
| `TAP_IAM_AUDIENCE` | — | Expected token audience; required unless `TAP_IAM_ALLOW_ANY_AUDIENCE=true` |
| `TAP_IAM_GROUP_CLAIMS` | `groups,wlcg.groups` | Claims read as IAM group membership |
| `TAP_AUTH_ROLES` | `{}` | `iam-groups` policy, as JSON keyed by operation |
| `TAP_PERMISSIONS_API_URL` | — | SKA SRC Permissions API base URL (`permissions-api` plugin) |
| `TAP_LOG_LEVEL` | `INFO` | Level for the services' own records (bootstrap, legacy-table warnings, deletion audit trail); uvicorn's access log is separate |
