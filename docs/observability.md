# Logs, metrics and tracing

Every service logs through SRCNet's shared
[`ska-src-logging`](https://gitlab.com/ska-telescope/src/src-api/ska-src-api-logging),
so records carry the same fields and JSON shape as the rest of SRCNet, and
both expose Prometheus metrics.

The metric set is not generic. It is what recent performance work needed and
did not have: a total collapse above eight concurrent queries took a local
reproduction and a sampling profiler to diagnose, because the signal —
time spent waiting for a database connection — existed nowhere a deployment
could see it. Each metric below is a number somebody would otherwise have to
reproduce locally.

## Following one request

Every request gets an id. One supplied by the caller in `X-Request-ID` is
kept, so a client or gateway that already traces a request keeps its trace;
otherwise the service generates one. It comes back on the response, so a
caller can quote it when reporting a problem.

That id then follows the work, which is the point:

| Where | How it appears |
| --- | --- |
| API logs | `request_id` field on every record |
| The database | a `/* rid=… */` comment on the statement, so it shows in `pg_stat_activity.query` and the server log |
| The job | a `request_id` column on `uws.jobs` |
| Executor logs | `request_id`, alongside `job_id` and `owner_id` |

So a slow statement seen in `pg_stat_activity` names the request that caused
it, and an executor's records for a job name the API call that created it:

```console
$ curl -sD- -X POST "$TAP/tap/sync" -H 'X-Request-ID: probe-001' … | grep -i x-request-id
x-request-id: probe-001

# and in the executor, for a job created by that request
{"level": "INFO", "app_name": "tap-executor", "message": "job 98be95c1 completed (5 rows, OK)",
 "job_id": "98be95c1…", "owner_id": null, "request_id": "probe-async-002"}
```

The SQL comment is appended rather than prefixed, so a statement still starts
with its verb and anything reading the beginning of a query keeps working.

## Metrics

`GET /metrics` on the API, and port `9100` on the executor — it serves no API
of its own, so its metrics get a listener instead.

| Metric | Type | Why it is here |
| --- | --- | --- |
| `tap_db_pool_wait_seconds` | histogram | The pool is the real concurrency limit. This is the signal that made a collapse above 8 concurrent queries invisible until it was reproduced locally. The wait only — a connection held through a long download is not a busy pool |
| `tap_db_pool_exhausted_total` | counter | Requests answered `503` because no connection came free |
| `tap_db_connections_in_use` | gauge | How much of the pool this process is holding |
| `tap_query_duration_seconds{kind}` | histogram | Query time, `sync` and `async` separately — they have different limits and different users. Every query that ran is in it, including one that was aborted or abandoned: those were slow too, and dropping them would flatter the tail |
| `tap_jobs{phase}` | gauge | The job store by phase |
| `tap_oldest_queued_job_seconds` | gauge | Queue backlog. This is what to autoscale executors on |
| `tap_jobs_completed_total{phase}` | counter | Job outcomes: `COMPLETED`, `ERROR` and `ABORTED`, labelled with the phase the job actually reached |

Queue metrics are reported by the executor, because the queue is its subject.
Every replica reports the same figures, so aggregate them with `max()` — they
describe one shared queue, not each worker's share.

### What to alert on

- `tap_db_pool_exhausted_total` rising at all. The service is refusing work;
  raise `config.dbPoolMax`, `tapApi.workers` or `tapApi.replicas`.
- The tail of `tap_db_pool_wait_seconds` growing before that happens — the
  same condition, earlier.
- `tap_oldest_queued_job_seconds` growing without bound: executors cannot keep
  up, so add `tapExecutor.replicas`.

## Scraping it

The endpoints exist whatever you scrape with. The chart annotates both
Deployments for pod discovery:

```yaml
prometheus.io/scrape: "true"
prometheus.io/path: /metrics
prometheus.io/port: "8080"   # 9100 on the executor
```

Set `metrics.scrapeAnnotations: false` to keep an existing Prometheus from
picking them up. The chart's own Prometheus, below, is unaffected: it finds
the executor by the release's labels and the configured port, so turning the
annotations off does not blind the scraper the chart deployed on purpose.

### A Prometheus for testing

`docker compose up` includes one at <http://localhost:9090>, already scraping
both services — useful for seeing the metrics without a cluster:

```console
$ curl -s 'localhost:9090/api/v1/query?query=tap_jobs_completed_total' | jq -r '.data.result[].value[1]'
```

The chart can deploy one too, for a cluster that has none:

```yaml
prometheus:
  enabled: true    # off by default
```

It is deliberately unsuitable for production — one replica, `emptyDir`
storage, 24h retention — because a real deployment already runs Prometheus and
should scrape these endpoints with it. Two scrapers is one too many.

## Tracing

Off unless a collector is configured, so a deployment without one pays
nothing:

```yaml
tracing:
  otlpEndpoint: "http://opentelemetry-collector.monitoring:4317"
```

That turns on FastAPI instrumentation and exports spans over OTLP. The
service name is the release name unless `OTEL_SERVICE_NAME` says otherwise.

## Log format and redaction

JSON in a container, coloured console when a human is watching — decided by
whether stderr is a terminal, and overridable with `LOG_FORMAT`
(`json`/`console`). `TAP_LOG_LEVEL` still sets the level.

Redaction is on by default (`LOG_ENABLE_REDACTION`), which matters here
because requests carry bearer tokens: the library's filters keep them out of
records. `LOG_REDACTION_PATTERNS` adds site-specific patterns.
