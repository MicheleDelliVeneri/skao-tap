# egernia

A draft **IVOA TAP 1.1** (Table Access Protocol) server in Python, built as
microservices on a PostgreSQL backend.

The [Table Access Protocol](https://www.ivoa.net/documents/TAP/) is the IVOA
standard for querying tabular data — astronomical catalogues as well as
general database tables — through a uniform web-service interface. This
project implements the full TAP 1.1 endpoint set:

- **`/sync`** — synchronous ADQL queries with DALI parameter handling
- **`/async`** — asynchronous queries managed via the UWS 1.1 job pattern
- **VOSI** — `/capabilities` (TAPRegExt), `/availability`, `/tables`
  (VODataService)
- **`TAP_SCHEMA`** — self-describing metadata tables, queryable via ADQL
- **`/examples`** — DALI service-provided examples

## Reused building blocks

| Concern | Library |
|---|---|
| ADQL parsing & translation to PostgreSQL | vendored ADQL 2.1 fork of [`queryparser-python3`](https://github.com/aipescience/queryparser) (ANTLR-based, translates ADQL geometry to [pg_sphere](https://pgsphere.github.io/); fork in `libs/egernia-core/egernia_core/query/_adql/`, the non-ADQL parts still from PyPI) |
| VOTable serialization | hand-written streaming writer (`egernia_core.query.results`); astropy parses it in tests |
| HTTP layer | FastAPI / uvicorn |
| Job queue | PostgreSQL `FOR UPDATE SKIP LOCKED` — no extra broker |

## Quick start

```bash
docker compose up --build -d
./scripts/smoke_test.sh
```

Then query with any IVOA client (TOPCAT, PyVO, astroquery):

```python
import pyvo

svc = pyvo.dal.TAPService("http://localhost:8080/tap")
print(svc.search("SELECT TOP 5 * FROM ska.continuum_sources").to_table())
```

The [Quickstart](quickstart.md) takes this further — cone search, UWS jobs,
Parquet and Arrow output, TOPCAT, and ingesting metadata.
