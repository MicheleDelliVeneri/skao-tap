# Roadmap

Follow-up work is organized in four numbered packages, referenced by number
in issues, PRs and discussions.

## Package 1 — Typed, streaming result pipeline with Parquet ✅

*Delivered.*

- Result columns typed from the PostgreSQL cursor and enriched with
  unit/UCD/description from `TAP_SCHEMA.columns` for the tables the query
  touches (propagated into VOTable `FIELD`s, the JSON metadata, and
  Arrow/Parquet field metadata).
- Server-side cursors + chunked streaming serialization end to end: the
  sync endpoints stream HTTP responses; the executor streams into result
  files. Result sets are never fully materialized.
- New output formats: **Parquet** (`RESPONSEFORMAT=parquet`,
  `application/vnd.apache.parquet`, zstd row groups, DALI status in the
  file metadata) and **Arrow IPC** (`RESPONSEFORMAT=arrow`), declared in
  the TAPRegExt capabilities.
- DALI overflow while streaming is reported with a trailing
  `QUERY_STATUS=OVERFLOW` INFO after the table.

## Package 2 — TAP table upload (`UPLOAD`)

- Inline uploads (multipart VOTable) and `http`/`https` URI uploads.
- Per-job `TAP_UPLOAD` temporary tables; ADQL translation and the
  published-table check made upload-aware.
- `uploadMethods` declared in capabilities; upload limits configurable.

## Package 3 — UWS completeness

- `WAIT` blocking requests (UWS 1.1) on the job and phase resources.
- `AFTER` filtering in the job list.
- Real `ABORT`: store the executing backend PID in `uws.jobs` and cancel
  the running statement with `pg_cancel_backend()`.

## Package 4 — Identity and registry

- Authentication and job ownership (`ownerId`), per-user visibility of the
  job list; groundwork for per-user schemas and quotas.
- VOResource record and VO Registry registration of the service.
