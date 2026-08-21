# skao-tap

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
|---|---|---|
| `db` | `db/` | PostgreSQL 16 + pg_sphere; init scripts create `TAP_SCHEMA`, a sample `ska.continuum_sources` catalogue, the `uws.jobs` table and a read-only `tap_reader` role used for all user queries |
| `tap-api` | `services/tap-api` | All TAP/UWS/VOSI HTTP endpoints |
| `tap-executor` | `services/tap-executor` | Asynchronous (UWS) query execution; multiple replicas can run concurrently |

Shared code lives in the `tapcore` package (`libs/tapcore`): configuration,
DB pool, ADQL translation, VOTable/CSV/TSV/JSON serialization, UWS job model
and XML rendering.

## Endpoints (TAP 1.1 / UWS 1.1 / VOSI / DALI)

| Endpoint | Standard | Notes |
|---|---|---|
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

## Known limitations of this draft

- **No table upload** (`UPLOAD` is rejected with a UsageFault and not
  declared in capabilities). TAP-mandated inline/HTTP upload is the first
  candidate for a follow-up.
- **No authentication** — all jobs are anonymous; `ownerId` is nil.
- Result sets are fully materialized in memory before serialization
  (fine for catalogue-scale drafts; switch to server-side cursors +
  streaming serialization for large tables).
- UWS `WAIT` (blocking requests, 1.1) and job list `AFTER` filtering are
  not implemented; `ABORT` marks the job but does not cancel the running
  backend statement.
- VOTable columns are typed by value inspection, not yet from
  `TAP_SCHEMA.columns` (so units/UCDs are not propagated into results).
- Registry registration (VOResource records) is out of scope here.
