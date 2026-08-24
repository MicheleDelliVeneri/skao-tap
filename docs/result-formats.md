# Result formats

Six writers produce the same rows: VOTable (the TAP default), CSV, TSV, JSON,
Parquet and an Arrow IPC stream. They are not equally cheap, and for a result
of ten thousand wide rows the difference between the cheapest and the most
expensive is most of the response time. This page says which to ask for and
why, with the measurement behind it.

Ask for one with `RESPONSEFORMAT` (or `FORMAT`) on `/sync`, or at submission
time on `/async`:

```bash
curl -sG "$TAP/sync" \
  --data-urlencode "LANG=ADQL" \
  --data-urlencode "QUERY=SELECT TOP 10000 * FROM ivoa.obscore" \
  --data-urlencode "RESPONSEFORMAT=parquet" -o result.parquet
```

## Which one to ask for

**Interactive queries and anything a VO client reads: VOTable.** It is the
default, every VO tool understands it, and it carries the units, UCDs and
descriptions from `TAP_SCHEMA`. It costs about a third more per row than
JSON and produces the largest body of the six.

**Bulk transfer — tens of thousands of rows and up: Parquet, or Arrow.** Both
are dramatically cheaper to produce per row than any text format, and Parquet
is dramatically smaller on the wire as well: the columns of an ObsCore result
compress extremely well, because a column of collection names or of
`calib_level` values is thousands of repetitions of a handful of distinct
values. Arrow is the cheapest of all to produce and streams uncompressed, so
it is the right choice when the bytes are going across a fast link into a
dataframe; Parquet is the right choice when they are going over a slow one or
onto disk.

Both stream in record batches and both carry the column units, UCDs and
descriptions as field metadata. Parquet additionally carries the DALI
`QUERY_STATUS` in its file metadata, so an overflowed result says so; the
Arrow IPC *stream* format has no end-of-stream metadata slot, so it cannot.

**Feeding a script or a spreadsheet: CSV or TSV.** Convenient, understood
everywhere, and the most expensive of the six per row — the cost is
CPython's `csv` writer, which has to consider quoting every field it writes.
Fine for a thousand rows; the wrong tool for a million.

**Feeding a web page: JSON.** The cheapest text format, and it carries the
column metadata in a `metadata` array beside the `data` array.

## What it costs

Measured with `make benchmark-serialize`, which runs the writers in process on
the Q11 projection — eleven ObsCore columns, five doubles, five wide text
fields, one small integer — with no database, no HTTP and no cluster in the
way, so a per-row cost is a per-row cost. 10,000 rows, best of 15 repetitions,
30-core host, Python 3.14:

| format | µs/row | bytes/row | relative cost |
| --- | --- | --- | --- |
| arrow | 0.93 | 236 | 1.0× |
| parquet | 1.78 | 52 | 1.9× |
| json | 5.12 | 296 | 5.5× |
| votable | 7.04 | 371 | 7.6× |
| tsv | 9.13 | 273 | 9.8× |
| csv | 9.19 | 273 | 9.9× |

Parquet is 1.9× the CPU of Arrow and one fifth of its bytes, which is the
trade the two of them represent. Everything textual is between five and ten
times the cheapest, and that is the cost of rendering numbers as decimal
digits — unavoidable in a text format, and the reason a bulk transfer should
not be one.

Reproduce it, or measure your own hardware:

```bash
make benchmark-serialize                      # 1,000 and 10,000 rows
make benchmark-serialize ROWS="1000 100000"   # or wherever your results live
```

The same comparison behind a real HTTP request — the writers plus the
database, the connection pool and the network — is
`make benchmark-result-formats`, which drives Q10 and Q11 through every format
at fixed concurrency and publishes a per-format p95 and response size. Use it
to decide what a *deployment* should recommend; use `benchmark-serialize` to
decide whether a change to the serialisation path did anything.

## How the writers stay cheap

Columns are typed once, from the PostgreSQL cursor's type OIDs, and the type
decides how a cell is rendered. That decision is made per **column**, not per
cell: a ten-thousand-row ObsCore result is 110,000 cells, and inspecting each
one's Python type to work out what it is — which is what the writers used to
do — costs more than everything else they do put together.

So a column's kind names the Python type psycopg produces as well as the wire
type the writer emits. `float8` arrives as a `float` and needs nothing done to
it; `numeric` arrives as a `Decimal` and becomes a double; a timestamp becomes
ISO-8601; text needs XML-escaping in VOTable and nothing anywhere else. The
kinds that need no conversion reach the writer untouched.

An OID the service does not recognise is `opaque`, and an `opaque` column
keeps the fully dynamic per-cell path — which is what it needs, because
nothing has promised what its values are. That is also the default for a
column whose type nobody stated. If you add a metadata domain with an exotic
column type, its results are correct and its serialisation is the slow one;
the fast path is a consequence of a type being recognised, not of anything
you have to configure.
