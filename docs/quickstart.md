# Quickstart

A working TAP service on your machine, queried from `curl`, PyVO and TOPCAT.
Everything here runs against the Docker Compose stack, which ships a sample
catalogue — `ska.continuum_sources` — so there is something to query before
you have loaded any data of your own.

## Prerequisites

Docker with the Compose plugin, and nothing else. The stack builds the two
services and initialises PostgreSQL 18 with pg_sphere from `db/init`.

## Start the stack

```bash
docker compose up --build -d
./scripts/smoke_test.sh
```

The smoke test checks availability, the table metadata, and one synchronous
and one asynchronous round trip. If it passes, the service is conformant
enough to talk to a real VO client.

The API is on `http://localhost:8080`, with the TAP base URL at
`http://localhost:8080/tap`.

## Your first query

TAP parameters follow DALI conventions, so names are case-insensitive and
travel either in the query string or in a form body:

```bash
curl "http://localhost:8080/tap/sync" \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT TOP 5 * FROM ska.continuum_sources" \
  --data-urlencode "RESPONSEFORMAT=csv"
```

Drop `RESPONSEFORMAT` and you get VOTable, which is the default because it is
what the standard and the existing VO clients expect.

## Cone search

ADQL geometry is translated to [pg_sphere](https://pgsphere.github.io/)
expressions, so positional queries are indexed rather than scanned:

```bash
curl "http://localhost:8080/tap/sync" \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT source_name, ra, dec FROM ska.continuum_sources
      WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 62.3, -65.5, 1.0))"
```

## Asynchronous queries

Long queries belong in a UWS job: the API queues it, an executor claims it,
and the result lands on the shared volume. Creating the job with `PHASE=RUN`
queues it immediately and redirects to the job URI.

```bash
JOB=$(curl -s -o /dev/null -w '%{redirect_url}' http://localhost:8080/tap/async \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT * FROM ska.continuum_sources" \
  --data-urlencode "PHASE=RUN")

curl "$JOB/phase"                 # PENDING → QUEUED → EXECUTING → COMPLETED
curl "$JOB/results/result"        # the result file
```

`PHASE=ABORT` on `$JOB/phase` cancels a running job in the database rather
than abandoning it, and `WAIT` blocks the request until the phase changes
instead of making you poll:

```bash
curl "$JOB?WAIT=30"               # returns early when the phase moves
```

## From PyVO

Any IVOA client works, because the service is discovered through its own
capabilities document rather than configured by hand:

```python
import pyvo

svc = pyvo.dal.TAPService("http://localhost:8080/tap")
print(svc.tables.keys())
print(svc.search("SELECT TOP 5 * FROM ska.continuum_sources").to_table())
```

An asynchronous job through the same client:

```python
job = svc.submit_job("SELECT * FROM ska.continuum_sources")
job.run().wait()
print(job.fetch_result().to_table())
```

## From TOPCAT

*VO → Table Access Protocol (TAP) Query*, then enter
`http://localhost:8080/tap` as the TAP URL. TOPCAT reads `/tables` for the
schema browser and `/examples` for the example queries shipped with the
service, so both appear without any extra configuration.

## Dataframe-native formats

`parquet` and `arrow` are first-class result formats, not exports bolted on
afterwards, and they carry the VO column semantics — `unit`, `ucd` and
`description` — as field metadata:

```bash
curl -o result.parquet "http://localhost:8080/tap/sync" \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT * FROM ska.continuum_sources" \
  --data-urlencode "RESPONSEFORMAT=parquet"
```

```python
import pandas as pd

df = pd.read_parquet("result.parquet")
```

For bulk transfer prefer these: a wide result is several times smaller as
zstd Parquet than as VOTable XML, and it lands directly in the dataframe
tooling that analysis actually uses.

## Ingest some metadata

Metadata domains accept JSON at `/api/v1/<domain>` and become queryable
through ordinary ADQL, because ingest registers the generated tables in
`TAP_SCHEMA`. The runnable
[PyVO notebook](https://github.com/ska-telescope/egernia/blob/main/demo/srcnet_metadata_tap.ipynb)
populates `srcnet.data_products` and `srcnet.software` against this same
Compose stack and queries both back through TAP.

## Stop the stack

```bash
docker compose down -v          # -v also drops the database volume
```

## Where to go next

- [Deployment](deployment.md) — Helm, scaling, backup and restore for a real
  installation; this Compose stack is not one
- [API reference](api.md) — every endpoint and parameter
- [Architecture](architecture.md) — why PostgreSQL is the only coordination
  point
- [Authentication](auth.md) — the Compose stack is anonymous; this is how to
  gate it
- [Metadata plugins](plugins.md) — binding your own pydantic model to a
  queryable schema
- [Development](development.md) — the `uv` workspace, tests and docs
