# skao-tap

[![CI](https://github.com/MicheleDelliVeneri/skao-tap/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/MicheleDelliVeneri/skao-tap/actions/workflows/ci.yml)
[![Security](https://github.com/MicheleDelliVeneri/skao-tap/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/MicheleDelliVeneri/skao-tap/actions/workflows/security.yml)
[![Docs](https://github.com/MicheleDelliVeneri/skao-tap/actions/workflows/docs.yml/badge.svg?branch=main)](https://github.com/MicheleDelliVeneri/skao-tap/actions/workflows/docs.yml)
[![Documentation](https://img.shields.io/badge/docs-github.io-blue)](https://micheledelliveneri.github.io/skao-tap/)
[![codecov](https://codecov.io/gh/MicheleDelliVeneri/skao-tap/branch/main/graph/badge.svg)](https://codecov.io/gh/MicheleDelliVeneri/skao-tap)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Python](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A draft **IVOA TAP 1.1** (Table Access Protocol) server in Python, built as
microservices on a PostgreSQL backend, reusing existing libraries for the
hard parts:

- **ADQL parsing/translation** — [`queryparser-python3`](https://github.com/aipescience/queryparser)
  (the ANTLR-based ADQL → PostgreSQL translator used by AIP's Daiquiri
  framework); geometry (`POINT`/`CIRCLE`/`CONTAINS`…) is translated to
  [pg_sphere](https://pgsphere.github.io/) expressions.
- **VOTable output** — `astropy.io.votable`.
- **Web framework** — FastAPI/uvicorn.
- **Job persistence & queue** — PostgreSQL (`FOR UPDATE SKIP LOCKED`), no
  extra broker needed.

## Architecture

```
                 ┌──────────────────────────┐
   TOPCAT/PyVO ─▶│  tap-api  (FastAPI)      │  /sync, /async (UWS REST),
   curl          │  validates params,       │  /capabilities, /availability,
                 │  translates ADQL,        │  /tables, /examples
                 │  runs sync queries,      │
                 │  serves job results      │
                 └─────┬──────────────┬─────┘
                       │ uws.jobs     │ results files
                       ▼              ▼
                 ┌───────────┐  ┌───────────────┐
                 │ PostgreSQL │  │ shared volume │
                 │ + pg_sphere│  │   /results    │
                 │ TAP_SCHEMA │  └───────▲───────┘
                 │ science    │          │ writes result.{vot,csv,...}
                 │ uws.jobs   │          │
                 └─────▲──────┘  ┌───────┴────────┐
                       └─────────│ tap-executor    │ claims QUEUED jobs
                                 │ (worker, scales │ (SKIP LOCKED), runs
                                 │  horizontally)  │ query, finalizes job
                                 └────────────────┘
```

Three services (see `docker-compose.yml`):

| Service | Code | Role |
| --- | --- | --- |
| `db` | `db/` | PostgreSQL 18 + pg_sphere; init scripts create `TAP_SCHEMA`, a sample `ska.continuum_sources` catalogue, the `uws.jobs` table and a read-only `tap_reader` role used for all user queries |
| `tap-api` | `services/tap-api` | All TAP/UWS/VOSI HTTP endpoints |
| `tap-executor` | `services/tap-executor` | Asynchronous (UWS) query execution; multiple replicas can run concurrently |

Shared code lives in the `tapcore` package (`libs/tapcore`): configuration,
DB pool, ADQL translation, VOTable/CSV/TSV/JSON serialization, UWS job model
and XML rendering.

## Endpoints (TAP 1.1 / UWS 1.1 / VOSI / DALI)

| Endpoint | Standard | Notes |
| --- | --- | --- |
| `GET/POST /tap/sync` | TAP | `LANG=ADQL`, `QUERY=...`, optional `RESPONSEFORMAT`, `MAXREC`; DALI error VOTables; `OVERFLOW` flagged |
| `GET/POST /tap/async` | TAP/UWS | job list (with `PHASE` filter) / job creation (303 → job URI); `PHASE=RUN` at creation queues immediately |
| `GET/POST/DELETE /tap/async/{id}` | UWS | job summary XML / `ACTION=DELETE` / delete |
| `GET/POST .../phase` | UWS | `PHASE=RUN` or `PHASE=ABORT` |
| `GET/POST .../executionduration` | UWS | per-job statement timeout (seconds) |
| `GET/POST .../destruction` | UWS | expired jobs are garbage-collected by the executor |
| `GET .../quote`, `.../owner` | UWS | |
| `GET/POST .../parameters` | UWS | updatable while `PENDING` |
| `GET .../results`, `.../results/result` | UWS | result document / result file |
| `GET .../error` | UWS/DALI | VOTable error document |
| `GET /tap/capabilities` | VOSI/TAPRegExt | languages, output formats, limits |
| `GET /tap/availability` | VOSI | checks database connectivity |
| `GET /tap/tables` | VOSI/VODataService | generated from `TAP_SCHEMA` |
| `GET /tap/examples` | DALI | RDFa examples (picked up by TOPCAT) |
| `GET /tap/registry` | VOResource | registration record; `404` until `voRegistry.enabled` |
| `TAP_SCHEMA.schemas/tables/columns/keys/key_columns` | TAP | self-describing, queryable via ADQL |

## Quickstart

```bash
docker compose up --build -d
./scripts/smoke_test.sh          # availability, tables, sync + async round trip
```

Synchronous query:

```bash
curl "http://localhost:8080/tap/sync" \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT TOP 5 * FROM ska.continuum_sources" \
  --data-urlencode "RESPONSEFORMAT=csv"
```

Cone search (ADQL geometry → pg_sphere):

```bash
curl "http://localhost:8080/tap/sync" \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT source_name, ra, dec FROM ska.continuum_sources
      WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 62.3, -65.5, 1.0))"
```

Asynchronous (UWS) query:

```bash
JOB=$(curl -s -o /dev/null -w '%{redirect_url}' http://localhost:8080/tap/async \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT * FROM ska.continuum_sources" \
  --data-urlencode "PHASE=RUN")
curl "$JOB/phase"                 # PENDING → QUEUED → EXECUTING → COMPLETED
curl "$JOB/results/result"        # VOTable result
```

From PyVO:

```python
import pyvo

svc = pyvo.dal.TAPService("http://localhost:8080/tap")
print(svc.search("SELECT TOP 5 * FROM ska.continuum_sources").to_table())
```

## Security model for user queries

User ADQL is never executed with service privileges:

1. `queryparser` accepts only the ADQL grammar (single `SELECT` statements).
2. Translated SQL is checked against the tables published in `TAP_SCHEMA`.
3. Execution happens inside a transaction with `SET LOCAL ROLE tap_reader`
   (SELECT-only role) and `SET LOCAL statement_timeout` (sync limit or the
   job's `executionDuration`).
4. `MAXREC` is enforced with a wrapping `LIMIT` (default 10 000, hard limit
   1 000 000; overflow is reported per DALI).

## Configuration

All via environment variables (see `tapcore/config.py`): `TAP_DATABASE_URL`,
`TAP_BASE_URL`, `TAP_RESULTS_DIR`, `TAP_QUERY_ROLE`, `TAP_DEFAULT_MAXREC`,
`TAP_HARD_MAXREC`, `TAP_SYNC_TIMEOUT`, `TAP_ASYNC_EXEC_DURATION`,
`TAP_JOB_RETENTION`.

## JSON API for machine-to-machine use

Alongside the standards-mandated XML of TAP, a JSON interface lives at
`/api/v1` (OpenAPI at `/openapi.json`): synchronous queries
(`POST /api/v1/query`), a JSON job facade over the same UWS store
(`/api/v1/jobs`), TAP_SCHEMA as JSON (`/api/v1/tables`), and the metadata
domains described below. See `docs/json-api.md`.

## Metadata plugins

Metadata domains are **plugins**: each binds a pydantic data model package
to a SQL schema and a JSON mount point, and the shared machinery generates
the tables, registers them in TAP_SCHEMA (so the metadata is queryable
through ordinary ADQL), migrates them when the model gains fields, and
serves ingest/fetch/amend endpoints. Two ship built in — **observatory
data products** ([ska-src-mm-notification](https://gitlab.com/ska-telescope/src/src-mm/ska-src-mm-notification),
`srcnet.projects` … `srcnet.artifacts`, `POST /api/v1/notifications`) and **software discovery**
([ska-src-sdm](https://gitlab.com/ska-telescope/src/src-mm/ska-src-mm-software-data-model),
`srcnet.software`, `POST /api/v1/software`).

Third-party model packages register through the `skao_tap.models`
entry-point group — installed alongside the services, no changes here —
and `TAP_MODEL_PLUGINS` selects which plugins a deployment activates
(`all`, or a subset for dedicated per-model systems). See
[`docs/plugins.md`](docs/plugins.md); the
[generated model-schema reference](docs/model-schemas.md) lists every
column directly from the installed data models.

A runnable [PyVO notebook](demo/srcnet_metadata_tap.ipynb) populates
`srcnet.data_products` and `srcnet.software` against the Docker Compose
service and queries both through TAP.

## Development

The repo is a [uv](https://docs.astral.sh/uv/) workspace
(`libs/tapcore`, `services/tap-api`, `services/tap-executor`):

```bash
uv sync --all-groups                 # environment with dev + docs groups
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/unit             # tapcore/tap-api unit tests
uv run pytest tests/component       # boots the stack, exercised with PyVO
uv run --group docs mkdocs serve     # documentation (mkdocs-material)
```

The component tests use **PyVO** — a standard IVOA client — to verify the
service behaves as the TAP/UWS/VOSI/DALI specs require: capabilities and
table metadata, sync queries (formats, `MAXREC`/overflow, geometry,
`TAP_SCHEMA`), DALI error documents, and the full UWS job lifecycle. They
need a reachable PostgreSQL (see `docs/development.md`) and skip otherwise.

CI (`.github/workflows/ci.yml`) runs lint, unit, component, docs, and helm
checks on every push/PR, and builds/pushes the three container images to
GHCR on `main`. Docs deploy to GitHub Pages via `docs.yml`.

## Kubernetes deployment (Helm)

```bash
helm upgrade --install skao-tap deploy/helm/skao-tap \
  --namespace skao-tap --create-namespace \
  --set tapApi.baseUrl=https://tap.example.org/tap
helm test skao-tap -n skao-tap       # in-cluster VOSI + sync smoke test
```

The chart deploys `tap-api` (+ Service/optional Ingress), `tap-executor`
(scale-out safe), an optional in-chart PostgreSQL 18 + pg_sphere
StatefulSet initialized from the same `db/init` SQL, and a shared results
PVC (use a ReadWriteMany storage class for multi-node clusters). Resilience
and operations are chart values: default anti-affinity/zone spread and
PodDisruptionBudgets for multi-replica services, opt-in
VerticalPodAutoscalers, `postgresql.tuning` server arguments, and a
scheduled `pg_dump` backup CronJob — with HA-PostgreSQL, PITR and restore
procedures documented in `docs/deployment.md`.

## Known limitations of this draft

Follow-up work is tracked as numbered packages in `docs/roadmap.md`.

- UWS `WAIT` (blocking requests, 1.1) and job list `AFTER` filtering are
  not implemented; `ABORT` marks the job but does not cancel the running
  backend statement — package 3.
- **Authentication is off by default** — a deployment that configures no IAM
  is fully anonymous, including the mutating metadata endpoints, so it must
  not be exposed to untrusted networks. Setting `auth.enabled=true` with an
  IAM issuer gates `POST`/`PATCH`/`DELETE` on `/api/v1/<mount>` behind
  verified bearer tokens, either from IAM group membership or via the
  [SKA SRC Permissions API](https://gitlab.com/ska-telescope/src/src-service-apis/ska-src-permissions-api)
  — see [docs/auth.md](docs/auth.md).
- **Service-local logging** — logs are plain `logging` records, outside the
  shared SRCNet observability stack; package 8 adopts
  [`ska-src-logging`](https://gitlab.com/ska-telescope/src/src-api/ska-src-api-logging)
  for structured logs, `X-Request-ID` correlation, traces and metrics.

Resolved by package 1: results now stream from server-side cursors (never
fully materialized), columns are typed from the cursor and carry
units/UCDs/descriptions from `TAP_SCHEMA.columns`, and **Parquet** and
**Arrow IPC** are available via `RESPONSEFORMAT=parquet|arrow` alongside
VOTable/CSV/TSV/JSON.

Resolved by package 2: **table upload** (`UPLOAD`) is supported on /sync
and /async — inline multipart VOTables (`param:`) and `http(s)` URIs,
queried as `TAP_UPLOAD.<name>` via per-query temporary tables, with
`uploadMethods` and the row limit declared in capabilities
(TABLEDATA serialization only; limits via `TAP_UPLOAD_MAX_ROWS` /
`TAP_UPLOAD_MAX_BYTES`).
