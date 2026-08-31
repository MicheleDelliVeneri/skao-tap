# Slow readers and collection listings — 20260831T141551Z

Run `20260831T141551Z-a042537-slow-readers-listings` · base commit `a042537` (working tree contained the step-7 changes) · 2026-08-31 14:15 UTC

## Headline

| finding | value |
| --- | ---: |
| point query wait with two slow readers retaining both pool slots | 686.93 ms |
| point query wait after readers spool before socket-paced delivery | 2.99 ms |
| 100k-job list, default 100 rows | 5.04 ms |
| 100k-job list, maximum 1,000 rows | 5.44 ms |
| 100k-job unbounded baseline, 88,889 visible rows | 22.90 ms |
| 100k-document first page, 101 rows including lookahead | 1.18 ms |
| 100k-document cursor page, 101 rows including lookahead | 1.07 ms |

## Method

The local Docker PostgreSQL database was populated inside a rolled-back transaction with 100,000 synthetic UWS jobs. Every ninth job was archived. The bounded and unbounded queries were each fetched completely; values are medians of five repetitions except the unbounded baseline, which used three.

Temporary metadata tables held 100,000 roots and 200,000 descendants behind a composite primary key. The query performed the production path's correlated descendant count for each root. Each first/midpoint keyset page requested 101 rows: 100 returned rows plus one lookahead row to decide whether `next_after` is present.

The slow-reader check used a two-connection `psycopg_pool` pool and two reader threads. Each reader paced 100 rows at 5 ms per row. The baseline held its connection during pacing. The comparison copied the result to `tempfile.SpooledTemporaryFile`, released the connection, and then paced reads from the spool. A point query was issued only after both readers had reached the measured state. Unit regressions separately serialize more than 2 MiB through the production `run_sync` path to prove disk rollover and cleanup, and force disconnect, byte-limit, and write-failure paths.

## Decision

- Synchronous results spool with a 1 MiB memory threshold and a 64 MiB serialized-response cap before delivery, releasing PostgreSQL before a slow client controls pacing. Disconnects cancel spool filling and the PostgreSQL statement.
- UWS lists return 100 jobs by default and never more than 1,000.
- Metadata lists return 100 documents by default, accept `limit` up to 1,000, and use root-ID keyset pagination through `after`/`next_after`.

## Scope and environment

This is a focused capacity diagnostic, not a production throughput claim. Rows were synthetic and narrow; PostgreSQL and the client ran on one host, so absolute latency is not portable. The pool-retention result is the relevant directional evidence.

| property | value |
| --- | --- |
| host | macOS 26.6.2, arm64, 14 logical CPUs |
| PostgreSQL | 18.6, Debian container |
| Python | 3.14.7 |
| pool | 2 connections |
| repetitions | 5 (3 for unbounded UWS baseline) |

Reproduce with:

```console
uv run python tests/performance/slow_readers_listings.py \
  --output docs/performance/20260831T141551Z-a042537-slow-readers-listings
```

[Raw summary](summary.csv) · [Environment](environment.json)
