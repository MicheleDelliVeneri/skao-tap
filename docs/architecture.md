# Architecture

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

## Services

| Service | Code | Role |
|---|---|---|
| `db` | `db/` | PostgreSQL 16 + pg_sphere; init scripts create `TAP_SCHEMA`, a sample `ska.continuum_sources` catalogue, the `uws.jobs` table and a read-only `tap_reader` role |
| `tap-api` | `services/tap-api` | All TAP/UWS/VOSI HTTP endpoints |
| `tap-executor` | `services/tap-executor` | Asynchronous (UWS) query execution; replicas can run concurrently |

Shared code lives in the **`tapcore`** package (`libs/tapcore`):
configuration, DB pool, ADQL translation, VOTable/CSV/TSV/JSON
serialization, the UWS job model and its XML rendering.

## Query lifecycle

**Synchronous** — `tap-api` validates the DALI parameters, translates ADQL
to PostgreSQL, checks the touched tables against `TAP_SCHEMA`, then executes
inside a transaction with `SET LOCAL ROLE tap_reader` and a statement
timeout. Results are serialized straight into the HTTP response, with
`MAXREC` enforced by a wrapping `LIMIT` and DALI `OVERFLOW` reporting.

**Asynchronous (UWS)** — job creation stores the parameters in `uws.jobs`
(`PENDING`). `PHASE=RUN` validates + translates the query and marks the job
`QUEUED`. Any `tap-executor` replica claims it with
`SELECT ... FOR UPDATE SKIP LOCKED` (moving it to `EXECUTING`), runs the
query under the job's `executionDuration` timeout, writes the result file to
the shared results volume, and finalizes the job (`COMPLETED`/`ERROR`).
Expired jobs (past their `destruction` time) are garbage-collected together
with their result files.

## Security model for user queries

1. `queryparser` accepts only the ADQL grammar (single `SELECT` statements).
2. Translated SQL is checked against the tables published in `TAP_SCHEMA`.
3. Execution happens under `SET LOCAL ROLE tap_reader` (SELECT-only) with a
   statement timeout.
4. `MAXREC` is enforced with a wrapping `LIMIT` (defaults: 10 000 soft,
   1 000 000 hard).
