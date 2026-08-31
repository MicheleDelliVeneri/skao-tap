# API reference

All resources live under the TAP base URL (default
`http://localhost:8080/tap`). Parameters follow DALI conventions:
case-insensitive names, sent via query string (GET) or form body (POST).
Errors are returned as DALI VOTable documents with
`INFO name="QUERY_STATUS" value="ERROR"` and HTTP status 400/404.

## Synchronous queries

| Method | Resource | Description |
|---|---|---|
| GET/POST | `/sync` | Execute an ADQL query; results spool before delivery and are limited by `TAP_SYNC_MAX_BYTES` |

Parameters:

| Name | Required | Description |
|---|---|---|
| `QUERY` | yes | The ADQL query |
| `LANG` | yes | `ADQL`, `ADQL-2.0`, or `ADQL-2.1` |
| `RESPONSEFORMAT` (or `FORMAT`) | no | `votable` (default), `csv`, `tsv`, `json`, `parquet`, `arrow`, or the equivalent MIME types |
| `MAXREC` | no | Row limit; `0` returns metadata only; overflow is flagged with `QUERY_STATUS=OVERFLOW` |
| `REQUEST` | no | `doQuery` accepted for TAP 1.0 compatibility |
| `UPLOAD` | no | Table upload: repeatable `name,uri` pairs separated by `;`. Use `param:<part>` for inline multipart VOTables. HTTP(S) sources are disabled unless their exact hosts are listed in `TAP_UPLOAD_ALLOWED_HOSTS`. Tables are queried as `TAP_UPLOAD.<name>`; TABLEDATA only. Limits: `TAP_UPLOAD_MAX_ROWS` (100000), `TAP_UPLOAD_MAX_BYTES` (32 MiB per source), `TAP_UPLOAD_MAX_TOTAL_BYTES` (32 MiB total), and `TAP_UPLOAD_MAX_SOURCES` (8). |

## Asynchronous queries (UWS 1.1)

| Method | Resource | Description |
|---|---|---|
| GET | `/async` | Job list (`PHASE` filter, `LAST` limit, `AFTER` ISO-8601 creation-time filter); returns `<uws:jobs>` with 100 jobs by default and at most 1,000 |
| POST | `/async` | Create a job from the same parameters as `/sync`; add `PHASE=RUN` to queue immediately; 303 → job URI |
| GET | `/async/{id}` | Job summary `<uws:job>` document; `WAIT=<s>` (or `-1` for the server maximum, `TAP_WAIT_MAX`) blocks until the phase changes, optionally with `PHASE=<phase>` as the reference phase |
| POST | `/async/{id}` | `ACTION=DELETE` destroys the job |
| DELETE | `/async/{id}` | Destroys the job; 303 → job list |
| GET/POST | `/async/{id}/phase` | Read phase (supports `WAIT`/`PHASE` blocking) / `PHASE=RUN` or `PHASE=ABORT`; ABORT cancels the running statement (`pg_cancel_backend`) |
| GET/POST | `/async/{id}/executionduration` | Per-job execution time limit (seconds), settable while `PENDING` |
| GET/POST | `/async/{id}/destruction` | Destruction time; expired jobs are garbage-collected |
| GET | `/async/{id}/quote` | Estimated completion time (nil in this draft) |
| GET | `/async/{id}/owner` | Job owner (anonymous in this draft) |
| GET/POST | `/async/{id}/parameters` | Read parameters / update them while `PENDING` |
| GET | `/async/{id}/results` | Result list; a completed job exposes one result named `result` |
| GET | `/async/{id}/results/result` | The result file, in the format requested at submission |
| GET | `/async/{id}/error` | VOTable error document for `ERROR` jobs |

Job phases: `PENDING → QUEUED → EXECUTING → COMPLETED | ERROR | ABORTED`
(`HELD`, `SUSPENDED`, `ARCHIVED` are accepted values per UWS).

## VOSI & metadata

| Method | Resource | Description |
|---|---|---|
| GET | `/capabilities` | TAPRegExt capability document: languages, output formats, limits |
| GET | `/availability` | Liveness (checks database connectivity) |
| GET | `/tables` | VODataService tableset generated from `TAP_SCHEMA` |
| GET | `/examples` | DALI-examples RDFa document |

`TAP_SCHEMA.schemas`, `.tables`, `.columns`, `.keys` and `.key_columns` are
regular published tables — query them via ADQL, e.g.

```sql
SELECT table_name, description FROM tap_schema.tables
```
