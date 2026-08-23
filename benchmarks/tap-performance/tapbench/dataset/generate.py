"""Grow the benchmark database to a series of measured sizes.

Size-driven, not row-driven: generation stops on ``pg_database_size()``, so
"D2 is 10 GiB" is a fact about the database rather than an estimate from a row
count and an assumed row width. One database is grown through every target and
checkpointed at each, so D4 costs 45 GiB of disk instead of D1+D2+D3+D4 = 82,
and each tier is a genuine prefix of the next — the same rows, more of them.

Resumable, because a run that dies at 30 GiB should not start again at zero:
every statement is idempotent on the primary key and the generator continues
from the highest observation index already present.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import time

import psycopg

log = logging.getLogger("tapbench.dataset")

HERE = pathlib.Path(__file__).parent

# The hierarchy is recomputed from the observation index at each level rather
# than read back from the table above it. That keeps the CAOM tables free of a
# synthetic join column they would not have in reality, and every level stays
# a pure function of (seed, index) — so a re-run of a batch writes the same
# rows, which is what makes ON CONFLICT DO NOTHING a resume rather than a
# corruption.
OBSERVATIONS = """
INSERT INTO caom.observation (
    obs_id, collection, telescope_name, instrument_name, target_name,
    intent, obs_type, proposal_id, sequence_number, meta_release,
    obs_release_date)
SELECT
    format('ska:obs:%%s', lpad(i::text, 12, '0')),
    bench.pick(%(seed)s, i, 'coll', %(collections)s),
    split_part(bench.pick(%(seed)s, i, 'coll', %(collections)s), '-', 1),
    bench.pick(%(seed)s, i, 'inst', %(instruments)s),
    format('FIELD-%%s', bench.rint(%(seed)s, i, 'target', 1, 40000)),
    CASE WHEN bench.rnd(%(seed)s, i, 'intent') < 0.85 THEN 'science' ELSE 'calibration' END,
    bench.pick(%(seed)s, i, 'otype', ARRAY['object', 'field', 'scan']),
    format('PROP-%%s-%%s', 2020 + bench.rint(%(seed)s, i, 'py', 0, 5),
           lpad(bench.rint(%(seed)s, i, 'pn', 1, 999)::text, 3, '0')),
    i,
    to_timestamp(%(mjd_lo)s * 86400.0 + bench.rnd(%(seed)s, i, 'mrel') * %(mjd_span)s * 86400.0
                 - 3506716800.0),
    to_timestamp(%(mjd_lo)s * 86400.0 + bench.rnd(%(seed)s, i, 'orel') * %(mjd_span)s * 86400.0
                 - 3506716800.0)
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i
ON CONFLICT (obs_id) DO NOTHING
"""

PLANES = """
INSERT INTO caom.plane (
    plane_id, obs_id, product_id, calib_level, data_product_type,
    time_bounds_lower, time_bounds_upper, time_exposure,
    energy_bounds_lower, energy_bounds_upper,
    position_ra, position_dec, position_sample_size, data_release)
SELECT
    format('ska:plane:%%s:%%s', lpad(i::text, 12, '0'), k),
    format('ska:obs:%%s', lpad(i::text, 12, '0')),
    format('product-%%s', k),
    bench.rint(%(seed)s, i * 8 + k, 'calib', 0, 3),
    bench.pick(%(seed)s, i * 8 + k, 'dptype', %(dataproduct_types)s),
    t.t_min, t.t_min + t.exptime / 86400.0, t.exptime,
    e.em_min, e.em_min * 1.35,
    360.0 * bench.rnd(%(seed)s, i, 'ra'),
    bench.sky_dec(%(seed)s, i),
    0.0001 + bench.rnd(%(seed)s, i * 8 + k, 'sample') * 0.01,
    to_timestamp(%(mjd_lo)s * 86400.0 + bench.rnd(%(seed)s, i * 8 + k, 'drel')
                 * %(mjd_span)s * 86400.0 - 3506716800.0)
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i
CROSS JOIN LATERAL generate_series(1, bench.rint(%(seed)s, i, 'nplane', 1, 3)) AS k
CROSS JOIN LATERAL (
    SELECT %(mjd_lo)s + bench.rnd(%(seed)s, i * 8 + k, 'tmin') * %(mjd_span)s AS t_min,
           10.0 + bench.rnd(%(seed)s, i * 8 + k, 'exp') * 28790.0 AS exptime) AS t
CROSS JOIN LATERAL (
    SELECT 0.0002 + bench.rnd(%(seed)s, i * 8 + k, 'em') * 0.2 AS em_min) AS e
ON CONFLICT (plane_id) DO NOTHING
"""

# ObsCore is plane-level, and deliberately wide: s_region, access_url and
# obs_publisher_did are what decide how many rows fit in a page, and page
# count is most of what an I/O benchmark measures.
OBSCORE = """
INSERT INTO ivoa.obscore (
    obs_publisher_did, obs_id, obs_collection, dataproduct_type, calib_level,
    target_name, facility_name, instrument_name,
    s_ra, s_dec, s_fov, s_region, s_resolution,
    t_min, t_max, t_exptime, t_resolution,
    em_min, em_max, em_res_power, o_ucd, pol_states,
    access_url, access_format, access_estsize)
SELECT
    format('ivo://skao.int/srcnet?ska:obs:%%s/product-%%s', lpad(i::text, 12, '0'), k),
    format('ska:obs:%%s', lpad(i::text, 12, '0')),
    bench.pick(%(seed)s, i, 'coll', %(collections)s),
    bench.pick(%(seed)s, i * 8 + k, 'dptype', %(dataproduct_types)s),
    bench.rint(%(seed)s, i * 8 + k, 'calib', 0, 3),
    format('FIELD-%%s', bench.rint(%(seed)s, i, 'target', 1, 40000)),
    split_part(bench.pick(%(seed)s, i, 'coll', %(collections)s), '-', 1),
    bench.pick(%(seed)s, i, 'inst', %(instruments)s),
    p.ra, p.dec, p.fov,
    format('CIRCLE ICRS %%s %%s %%s', round(p.ra::numeric, 5), round(p.dec::numeric, 5),
           round((p.fov / 2.0)::numeric, 5)),
    p.fov / 40.0,
    t.t_min, t.t_min + t.exptime / 86400.0, t.exptime, t.exptime / 8.0,
    e.em_min, e.em_min * 1.35, 1000.0 + bench.rnd(%(seed)s, i * 8 + k, 'emres') * 9000.0,
    'em.radio', bench.pick(%(seed)s, i * 8 + k, 'pol', ARRAY['I', 'I/Q/U/V', 'I/V']),
    format('https://data.srcnet.skao.int/artifact/ska:obs:%%s/product-%%s/data.fits',
           lpad(i::text, 12, '0'), k),
    'application/fits',
    (1000000 + bench.rnd(%(seed)s, i * 8 + k, 'size') * 4000000000)::bigint
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i
CROSS JOIN LATERAL generate_series(1, bench.rint(%(seed)s, i, 'nplane', 1, 3)) AS k
CROSS JOIN LATERAL (
    SELECT 360.0 * bench.rnd(%(seed)s, i, 'ra') AS ra,
           bench.sky_dec(%(seed)s, i) AS dec,
           0.02 + bench.rnd(%(seed)s, i * 8 + k, 'fov') * 4.0 AS fov) AS p
CROSS JOIN LATERAL (
    SELECT %(mjd_lo)s + bench.rnd(%(seed)s, i * 8 + k, 'tmin') * %(mjd_span)s AS t_min,
           10.0 + bench.rnd(%(seed)s, i * 8 + k, 'exp') * 28790.0 AS exptime) AS t
CROSS JOIN LATERAL (
    SELECT 0.0002 + bench.rnd(%(seed)s, i * 8 + k, 'em') * 0.2 AS em_min) AS e
ON CONFLICT (obs_publisher_did) DO NOTHING
"""

ARTIFACTS = """
INSERT INTO caom.artifact (
    artifact_id, plane_id, uri, product_type, content_type, content_length,
    release_type)
SELECT
    format('ska:artifact:%%s:%%s:%%s', lpad(i::text, 12, '0'), k, a),
    format('ska:plane:%%s:%%s', lpad(i::text, 12, '0'), k),
    format('https://data.srcnet.skao.int/artifact/ska:obs:%%s/product-%%s/part-%%s.fits',
           lpad(i::text, 12, '0'), k, a),
    bench.pick(%(seed)s, i * 64 + k * 8 + a, 'ptype',
               ARRAY['science', 'calibration', 'preview', 'auxiliary']),
    'application/fits',
    (100000 + bench.rnd(%(seed)s, i * 64 + k * 8 + a, 'clen') * 2000000000)::bigint,
    'data'
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i
CROSS JOIN LATERAL generate_series(1, bench.rint(%(seed)s, i, 'nplane', 1, 3)) AS k
CROSS JOIN LATERAL generate_series(1, bench.rint(%(seed)s, i * 8 + k, 'nart', 1, 4)) AS a
ON CONFLICT (artifact_id) DO NOTHING
"""

PARTS = """
INSERT INTO caom.part (part_id, artifact_id, part_name, product_type)
SELECT
    format('ska:part:%%s:%%s:%%s:%%s', lpad(i::text, 12, '0'), k, a, pr),
    format('ska:artifact:%%s:%%s:%%s', lpad(i::text, 12, '0'), k, a),
    format('HDU%%s', pr),
    bench.pick(%(seed)s, i * 512 + k * 64 + a * 8 + pr, 'ptype',
               ARRAY['science', 'calibration', 'auxiliary'])
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i
CROSS JOIN LATERAL generate_series(1, bench.rint(%(seed)s, i, 'nplane', 1, 3)) AS k
CROSS JOIN LATERAL generate_series(1, bench.rint(%(seed)s, i * 8 + k, 'nart', 1, 4)) AS a
CROSS JOIN LATERAL generate_series(1, bench.rint(%(seed)s, i * 64 + k * 8 + a, 'npart', 1, 2)) AS pr
ON CONFLICT (part_id) DO NOTHING
"""

CHUNKS = """
INSERT INTO caom.chunk (
    chunk_id, part_id, naxis, position_axis_1, position_axis_2,
    energy_axis, time_axis, polarization_axis, observable_axis, sample_size)
SELECT
    format('ska:chunk:%%s:%%s:%%s:%%s:%%s', lpad(i::text, 12, '0'), k, a, pr, c),
    format('ska:part:%%s:%%s:%%s:%%s', lpad(i::text, 12, '0'), k, a, pr),
    bench.rint(%(seed)s, i * 4096 + k * 512 + a * 64 + pr * 8 + c, 'naxis', 2, 4),
    1, 2, 3, 4, 5, 6,
    0.0001 + bench.rnd(%(seed)s, i * 4096 + k * 512 + a * 64 + pr * 8 + c, 'ss') * 0.01
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i
CROSS JOIN LATERAL generate_series(1, bench.rint(%(seed)s, i, 'nplane', 1, 3)) AS k
CROSS JOIN LATERAL generate_series(1, bench.rint(%(seed)s, i * 8 + k, 'nart', 1, 4)) AS a
CROSS JOIN LATERAL generate_series(1, bench.rint(%(seed)s, i * 64 + k * 8 + a, 'npart', 1, 2)) AS pr
CROSS JOIN LATERAL generate_series(
    1, bench.rint(%(seed)s, i * 512 + k * 64 + a * 8 + pr, 'nchunk', 1, 2)) AS c
ON CONFLICT (chunk_id) DO NOTHING
"""

LEVELS = (
    ("observation", OBSERVATIONS),
    ("plane", PLANES),
    ("obscore", OBSCORE),
    ("artifact", ARTIFACTS),
    ("part", PARTS),
    ("chunk", CHUNKS),
)

TABLES = (
    "caom.observation",
    "caom.plane",
    "caom.artifact",
    "caom.part",
    "caom.chunk",
    "ivoa.obscore",
)

# Maintained during load, so the cone-search corpus has an index to use at
# every checkpoint. Dropped only while a tier is growing towards the next one:
# a GiST index maintained row-by-row through a hundred million inserts is the
# single slowest thing in this file, and rebuilding it once is minutes.
SPATIAL_INDEX = """
CREATE INDEX IF NOT EXISTS obscore_spoint_gist
    ON ivoa.obscore USING gist (spoint(radians(s_ra), radians(s_dec)))
"""


@dataclasses.dataclass
class DatasetStats:
    """What was actually built, as opposed to what was asked for."""

    name: str
    database_bytes: int
    table_bytes: dict[str, int]
    index_bytes: dict[str, int]
    row_counts: dict[str, int]
    obscore_rows: int
    index_to_table_ratio: float
    observations_generated: int
    seconds: float

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


def _params(cfg: dict, lo: int, hi: int) -> dict:
    gen = cfg["generation"]
    mjd_lo, mjd_hi = gen["mjd_range"]
    return {
        "seed": str(gen["seed"]),
        "lo": lo,
        "hi": hi,
        "collections": gen["collections"],
        "instruments": gen["instruments"],
        "dataproduct_types": gen["dataproduct_types"],
        "mjd_lo": mjd_lo,
        "mjd_span": mjd_hi - mjd_lo,
    }


def database_bytes(conn) -> int:
    return conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]


def highest_observation(conn) -> int:
    """Where a previous, interrupted generation got to."""
    row = conn.execute("SELECT max(sequence_number) FROM caom.observation").fetchone()
    return int(row[0] or 0)


def apply_schema(conn) -> None:
    for name in ("generate.sql", "schema.sql"):
        conn.execute((HERE / name).read_text())
    conn.execute(SPATIAL_INDEX)
    conn.commit()


def register_columns(conn) -> None:
    """Publish the columns in TAP_SCHEMA, read from the database itself.

    Introspected rather than written out by hand, and ``indexed`` is taken
    from pg_index rather than asserted: a hand-maintained copy of the DDL
    drifts, and an ``indexed`` flag that lies is worse than none — it is the
    column a client uses to decide what to filter on.
    """
    conn.execute(
        """
        WITH cols AS (
            SELECT c.table_schema, c.table_name, c.column_name, c.ordinal_position,
                   c.data_type,
                   format('%s.%s', c.table_schema, c.table_name) AS qualified
              FROM information_schema.columns c
             WHERE c.table_schema IN ('caom', 'ivoa')
        ), indexed AS (
            SELECT format('%s.%s', n.nspname, t.relname) AS qualified, a.attname
              FROM pg_index x
              JOIN pg_class t ON t.oid = x.indrelid
              JOIN pg_namespace n ON n.oid = t.relnamespace
              JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY (x.indkey)
             WHERE n.nspname IN ('caom', 'ivoa')
        )
        INSERT INTO tap_schema.columns
            (table_name, column_name, datatype, arraysize, description,
             indexed, principal, std, column_index)
        SELECT cols.qualified, cols.column_name,
               CASE
                   WHEN cols.data_type LIKE 'double%' THEN 'double'
                   WHEN cols.data_type = 'bigint' THEN 'long'
                   WHEN cols.data_type = 'smallint' THEN 'short'
                   WHEN cols.data_type = 'integer' THEN 'int'
                   WHEN cols.data_type LIKE 'timestamp%' THEN 'char'
                   ELSE 'char'
               END,
               CASE WHEN cols.data_type IN ('text', 'character varying')
                         OR cols.data_type LIKE 'timestamp%' THEN '*' END,
               format('synthetic benchmark column %s', cols.column_name),
               CASE WHEN EXISTS (SELECT 1 FROM indexed
                                  WHERE indexed.qualified = cols.qualified
                                    AND indexed.attname = cols.column_name)
                    THEN 1 ELSE 0 END,
               1, 0, cols.ordinal_position
          FROM cols
        ON CONFLICT (table_name, column_name) DO UPDATE
           SET indexed = excluded.indexed
        """
    )
    conn.commit()


def collect_stats(conn, name: str, observations: int, seconds: float) -> DatasetStats:
    table_bytes, index_bytes, rows = {}, {}, {}
    for table in TABLES:
        table_bytes[table] = conn.execute("SELECT pg_table_size(%s)", (table,)).fetchone()[0]
        index_bytes[table] = conn.execute("SELECT pg_indexes_size(%s)", (table,)).fetchone()[0]
        # Counted, not estimated: the row counts are reported as facts about
        # the dataset, and reltuples is a statistic that lags a load.
        rows[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    total_table = sum(table_bytes.values()) or 1
    return DatasetStats(
        name=name,
        database_bytes=database_bytes(conn),
        table_bytes=table_bytes,
        index_bytes=index_bytes,
        row_counts=rows,
        obscore_rows=rows["ivoa.obscore"],
        index_to_table_ratio=sum(index_bytes.values()) / total_table,
        observations_generated=observations,
        seconds=seconds,
    )


def grow_to(conn, cfg: dict, target_bytes: int, batch_rows: int) -> int:
    """Generate until the database reaches target_bytes. Returns observations."""
    index = highest_observation(conn)
    if index:
        log.info("resuming generation from observation %d", index)
    while True:
        size = database_bytes(conn)
        if size >= target_bytes:
            return index
        remaining = target_bytes - size
        log.info(
            "%.2f/%.2f GiB (%.1f%%), %d observations",
            size / 2**30,
            target_bytes / 2**30,
            100.0 * size / target_bytes,
            index,
        )
        lo, hi = index + 1, index + batch_rows
        for level, statement in LEVELS:
            with conn.cursor() as cur:
                cur.execute(statement, _params(cfg, lo, hi))
            conn.commit()
            del level
        index = hi
        # A batch that adds nothing would spin forever; that can only happen
        # if the statements stopped inserting, which is a bug rather than a
        # slow disk, so it is worth saying out loud.
        if database_bytes(conn) <= size:
            raise RuntimeError(
                f"batch {lo}-{hi} did not grow the database ({size} bytes); "
                "generation would not terminate"
            )
        del remaining


def build(dsn: str, cfg: dict, targets: list[dict], out_dir: pathlib.Path) -> list[DatasetStats]:
    """Grow through every target, checkpointing stats at each."""
    out_dir.mkdir(parents=True, exist_ok=True)
    built = []
    with psycopg.connect(dsn, autocommit=False) as conn:
        apply_schema(conn)
        register_columns(conn)
        for target in targets:
            marker = out_dir / f"{target['name']}.json"
            if marker.exists():
                log.info("%s already built, skipping", target["name"])
                built.append(DatasetStats(**json.loads(marker.read_text())))
                continue
            started = time.monotonic()
            observations = grow_to(
                conn, cfg, target["target_bytes"], cfg["generation"]["batch_rows"]
            )
            conn.execute(SPATIAL_INDEX)
            conn.commit()
            log.info("VACUUM ANALYZE for %s", target["name"])
            with psycopg.connect(dsn, autocommit=True) as vac:
                vac.execute("VACUUM ANALYZE")
            stats = collect_stats(conn, target["name"], observations, time.monotonic() - started)
            marker.write_text(json.dumps(stats.to_json(), indent=2, sort_keys=True))
            log.info(
                "%s built: %.2f GiB, %d obscore rows, index/table %.2f",
                stats.name,
                stats.database_bytes / 2**30,
                stats.obscore_rows,
                stats.index_to_table_ratio,
            )
            built.append(stats)
    return built
