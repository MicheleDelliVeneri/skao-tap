# Deployment

## Docker Compose (development)

```bash
docker compose up --build -d
./scripts/smoke_test.sh
```

Compose builds three images: `db` (PostgreSQL 16 + pg_sphere, initialized
from `db/init/*.sql`), `tap-api` and `tap-executor` (both installed with
`uv sync --frozen` from the committed `uv.lock`).

## Helm (Kubernetes)

A chart is provided under `deploy/helm/skao-tap`:

```bash
helm upgrade --install skao-tap deploy/helm/skao-tap \
  --namespace skao-tap --create-namespace \
  --set tapApi.baseUrl=https://tap.example.org/tap
helm test skao-tap -n skao-tap
```

Key values (see `values.yaml` for the full list):

| Value | Default | Description |
|---|---|---|
| `image.registry` / `image.tag` | `ghcr.io/micheledelliveneri` / chart appVersion | Where CI publishes the service images |
| `tapApi.replicas` | `1` | API replicas (stateless) |
| `tapApi.baseUrl` | in-cluster service URL | External base URL written into capabilities and result links |
| `tapExecutor.replicas` | `1` | Executor replicas; safe to scale out (jobs claimed with `SKIP LOCKED`) |
| `postgresql.enabled` | `true` | Deploy the in-chart PostgreSQL + pg_sphere; disable to use an external DB via `externalDatabase.url` |
| `results.storageClass` / `results.size` | `""` / `1Gi` | Shared results volume |
| `ingress.enabled` | `false` | Optional ingress for the API |

!!! warning "Results volume access mode"
    The results volume is shared between the API and the executor. With more
    than one node you need a `ReadWriteMany`-capable storage class (or pin
    both deployments to one node); the chart defaults to `ReadWriteOnce`
    which is only safe for single-node/dev clusters.

The in-chart PostgreSQL mounts the same `db/init` SQL (copied into the chart
at `files/db-init/`; CI verifies both copies stay in sync).

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

## Configuration

All services read environment variables (see `tapcore/config.py`):

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
