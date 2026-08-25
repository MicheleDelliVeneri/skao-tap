<p align="center">
  <img src="assets/egernia-logo.svg" alt="egernia — IVOA TAP 1.1 for the SKA" width="640">
</p>

<p align="center">
  An <b>IVOA TAP 1.1</b> server in Python — ADQL over PostgreSQL, scaling on nothing but the database.
</p>

<p align="center">
  <a href="https://micheledelliveneri.github.io/egernia/"><b>Documentation</b></a> ·
  <a href="https://micheledelliveneri.github.io/egernia/quickstart/">Quickstart</a> ·
  <a href="https://micheledelliveneri.github.io/egernia/architecture/">Architecture</a> ·
  <a href="https://micheledelliveneri.github.io/egernia/roadmap/">Roadmap</a>
</p>

<p align="center">
  <a href="https://github.com/MicheleDelliVeneri/egernia/actions/workflows/ci.yml"><img src="https://github.com/MicheleDelliVeneri/egernia/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/MicheleDelliVeneri/egernia/actions/workflows/security.yml"><img src="https://github.com/MicheleDelliVeneri/egernia/actions/workflows/security.yml/badge.svg?branch=main" alt="Security"></a>
  <a href="https://codecov.io/gh/MicheleDelliVeneri/egernia"><img src="https://codecov.io/gh/MicheleDelliVeneri/egernia/branch/main/graph/badge.svg" alt="Coverage"></a>
  <a href="https://micheledelliveneri.github.io/egernia/"><img src="https://img.shields.io/badge/docs-github.io-blue" alt="Docs"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.14%2B-blue" alt="Python 3.14+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license"></a>
</p>

---

The [Table Access Protocol][tap-std] is the IVOA standard for querying tabular
data — astronomical catalogues as well as ordinary database tables — over a
uniform web service. This is a complete TAP 1.1 implementation for the SKA
Regional Centre Network, built as two stateless services over one PostgreSQL
database.

## Why this exists

- **PostgreSQL is the only coordination point.** The job queue is
  `FOR UPDATE SKIP LOCKED` on a table, so there is no broker, no cache tier and
  no service registry to operate — and executors scale out with no leases and
  no split-brain.
- **User ADQL never runs with service privileges.** Queries are parsed as ADQL,
  checked against `TAP_SCHEMA`, then executed under `SET LOCAL ROLE tap_reader`
  with a statement timeout. An ADQL query cannot write, whatever it manages to
  express.
- **Everything streams.** Rows leave a server-side cursor and become HTTP
  chunks, so a ten-million-row result never exists in memory. **Parquet** and
  **Arrow** are first-class formats carrying units, UCDs and descriptions as
  field metadata — not exports bolted on afterwards.
- **Metadata domains are plugins.** A pydantic model package binds to a SQL
  schema and a JSON mount point; the shared machinery generates the tables,
  migrates them as the model grows, and registers them in `TAP_SCHEMA` so
  ingested metadata is queryable by ordinary ADQL.

## Quickstart

```bash
docker compose up --build -d
./scripts/smoke_test.sh          # availability, tables, sync + async round trip
```

Query it with `curl`:

```bash
curl "http://localhost:8080/tap/sync" \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT TOP 5 * FROM ska.continuum_sources" \
  --data-urlencode "RESPONSEFORMAT=csv"
```

…or with any IVOA client, which needs no configuration beyond the URL:

```python
import pyvo

svc = pyvo.dal.TAPService("http://localhost:8080/tap")
print(svc.search("SELECT TOP 5 * FROM ska.continuum_sources").to_table())
```

Cone search, UWS jobs, Parquet output, TOPCAT and metadata ingest are in the
[Quickstart guide][quickstart].

## Standards implemented

| Standard | Surface |
| --- | --- |
| **TAP 1.1** | `/sync`, `/async`, `TAP_SCHEMA`, `UPLOAD` (inline multipart and `http(s)`) |
| **ADQL 2.0** | Parsed and translated by [`queryparser-python3`][queryparser]; geometry becomes [pg_sphere][pgsphere] expressions |
| **UWS 1.1** | Complete job lifecycle, including `WAIT` blocking, `AFTER` filtering, and `ABORT` that cancels the running statement |
| **VOSI** | `/capabilities` (TAPRegExt), `/availability`, `/tables` (VODataService) |
| **DALI** | Parameter conventions, error VOTables, `/examples` |
| **VOTable 1.4** | via `astropy.io.votable`; plus CSV, TSV, JSON, Parquet, Arrow |
| **VOResource** | `/tap/registry` record for registry harvesting |
| **AuthVO** | Challenges naming the IAM, so a client can go and get a token |

Endpoint-by-endpoint detail with every parameter is in the [API reference][api].
A JSON interface for machine-to-machine use lives alongside the
standards-mandated XML at `/api/v1` — see the [JSON API][json-api].

## Deploy

```bash
helm upgrade --install egernia deploy/helm/egernia \
  --namespace egernia --create-namespace \
  --set tapApi.baseUrl=https://tap.example.org/tap
helm test egernia -n egernia       # in-cluster VOSI + sync smoke test
```

The chart covers both services, an optional in-chart PostgreSQL, anti-affinity
and PodDisruptionBudgets, opt-in autoscaling and scheduled backups. External HA
PostgreSQL, PITR, restore and hardening are in the [deployment guide][deploy].

> [!IMPORTANT]
> **Authentication is off by default.** A deployment that configures no IAM is
> fully anonymous, including the mutating metadata endpoints, so it must not be
> exposed to untrusted networks. See [Authentication][auth].

## Documentation

| | |
| --- | --- |
| [Quickstart][quickstart] | Run it locally, query it from `curl`, PyVO and TOPCAT |
| [Architecture][arch] | The two-service shape, request paths, failure behaviour |
| [API reference][api] · [JSON API][json-api] | Every endpoint and parameter |
| [Deployment][deploy] · [Autoscaling][autoscaling] | Helm values, scaling, backup, restore |
| [Authentication][auth] · [VO Registry][registry] | Tokens, gated operations, registration |
| [Metadata plugins][plugins] · [Model schemas][schemas] | Binding a pydantic model to a queryable schema |
| [Observability][obs] · [Benchmarking][bench] | Metrics, tracing, how the numbers were measured |
| [Development][dev] · [Roadmap][roadmap] | The `uv` workspace, tests, and what is planned |

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
development loop and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for the standards
we hold each other to. Questions and bug reports belong in
[GitHub issues](https://github.com/MicheleDelliVeneri/egernia/issues).

To report a security vulnerability, follow [SECURITY.md](SECURITY.md) rather
than opening a public issue.

## Citing

If this software supports published work, please cite it — GitHub builds the
citation from [CITATION.cff](CITATION.cff) via *Cite this repository*.

## License

[MIT](LICENSE) © Michele Delli Veneri and contributors.

[tap-std]: https://www.ivoa.net/documents/TAP/
[queryparser]: https://github.com/aipescience/queryparser
[pgsphere]: https://pgsphere.github.io/
[quickstart]: https://micheledelliveneri.github.io/egernia/quickstart/
[arch]: https://micheledelliveneri.github.io/egernia/architecture/
[api]: https://micheledelliveneri.github.io/egernia/api/
[json-api]: https://micheledelliveneri.github.io/egernia/json-api/
[deploy]: https://micheledelliveneri.github.io/egernia/deployment/
[autoscaling]: https://micheledelliveneri.github.io/egernia/autoscaling/
[auth]: https://micheledelliveneri.github.io/egernia/auth/
[registry]: https://micheledelliveneri.github.io/egernia/registry/
[plugins]: https://micheledelliveneri.github.io/egernia/plugins/
[schemas]: https://micheledelliveneri.github.io/egernia/model-schemas/
[obs]: https://micheledelliveneri.github.io/egernia/observability/
[bench]: https://micheledelliveneri.github.io/egernia/benchmarking/
[dev]: https://micheledelliveneri.github.io/egernia/development/
[roadmap]: https://micheledelliveneri.github.io/egernia/roadmap/
