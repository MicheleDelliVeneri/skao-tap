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

import contextlib
import dataclasses
import json
import logging
import pathlib
import time

import psycopg

log = logging.getLogger("egernia_dataset")

HERE = pathlib.Path(__file__).parent

# Where a load parks the index definitions it dropped, so a run that is
# killed rather than raised can still be recovered from by the next one.
STASH = "srcnet._bench_stashed_indexes"

# The hierarchy is recomputed from the project index at each level rather than
# read back from the table above it. That keeps the ODP tables free of a
# synthetic join column the model does not have, and every level stays a pure
# function of (seed, index) — so a re-run of a batch writes the same rows,
# which is what makes ON CONFLICT DO NOTHING a resume rather than a
# corruption.
# One project's fan-out, and the arithmetic that makes every level derivable
# from the project index alone. A project yields 4 observations, each split
# into 2 scheduling blocks, each executed twice, each execution producing 8
# data products with 2 artifacts apiece: 128 products and 256 artifacts per
# project. Chosen so the joins cost something and a project reads like an
# observing programme, not to hit a row count.
OBS_PER_PROJECT = 4
SBD_PER_OBS = 2
EB_PER_SBD = 2
PRODUCTS_PER_EB = 8
ARTIFACTS_PER_PRODUCT = 2

# Identifier formats, shared by every level so the keys line up without any
# level reading the one above it. The width is fixed: these are text primary
# keys and a b-tree over them is part of what is being measured.
_PROJECT_ID = "format('SKAO-P%%s', lpad(i::text, 9, '0'))"
_OBS_ID = (
    "format('%%s-%%s-%%s', bench.pick(%(seed)s, i * 8 + o, 'inst', %(instruments)s),"
    " lpad(i::text, 9, '0'), lpad(o::text, 2, '0'))"
)
_SBD_ID = (
    "format('sbd-%%s-%%s-%%s', lpad(i::text, 9, '0'), lpad(o::text, 2, '0'), lpad(s::text, 2, '0'))"
)
_EB_ID = (
    "format('eb-%%s-%%s-%%s-%%s', lpad(i::text, 9, '0'), lpad(o::text, 2, '0'),"
    " lpad(s::text, 2, '0'), lpad(e::text, 3, '0'))"
)

# A distinct deterministic stream per row at each level: bench.rnd is keyed on
# a single bigint, so the child indices are folded into it. Without this every
# data product of one execution block would draw the same "random" values.
_PRODUCT_IX = "((((i * 8 + o) * 4 + s) * 4 + e) * 16 + p)"

PROJECTS = f"""
INSERT INTO srcnet.projects (
    schema_version, project_id, group_ids, project_title, pi_name, data_rights)
SELECT
    '2.1',
    {_PROJECT_ID},
    to_jsonb(ARRAY[format('SKAO-P%%s_group', lpad(i::text, 9, '0'))]),
    format('%%s programme %%s', bench.pick(%(seed)s, i, 'sci', %(science)s), i),
    format('Dr. %%s', bench.pick(%(seed)s, i, 'pi', %(surnames)s)),
    bench.pick(%(seed)s, i, 'rights', ARRAY['public', 'proprietary', 'private'])
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i
ON CONFLICT (project_id) DO NOTHING
"""

OBSERVATIONS = f"""
INSERT INTO srcnet.observations (
    project_id, obs_id, obs_title, collection, instrument_name, facility_name)
SELECT
    {_PROJECT_ID},
    {_OBS_ID},
    format('%%s observation of %%s',
           bench.pick(%(seed)s, i * 8 + o, 'sci', %(science)s),
           bench.pick(%(seed)s, i * 8 + o, 'target', %(targets)s)),
    format('SKAO/%%s', bench.pick(%(seed)s, i * 8 + o, 'inst', %(instruments)s)),
    bench.pick(%(seed)s, i * 8 + o, 'inst', %(instruments)s),
    'Square Kilometre Array Observatory'
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i,
     generate_series(0, {OBS_PER_PROJECT - 1}) AS o
ON CONFLICT (project_id, obs_id) DO NOTHING
"""

SCHEDULING_BLOCKS = f"""
INSERT INTO srcnet.scheduling_blocks (project_id, obs_id, sbd_id)
SELECT {_PROJECT_ID}, {_OBS_ID}, {_SBD_ID}
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i,
     generate_series(0, {OBS_PER_PROJECT - 1}) AS o,
     generate_series(0, {SBD_PER_OBS - 1}) AS s
ON CONFLICT (project_id, obs_id, sbd_id) DO NOTHING
"""

EXECUTION_BLOCKS = f"""
INSERT INTO srcnet.execution_blocks (project_id, obs_id, sbd_id, eb_id)
SELECT {_PROJECT_ID}, {_OBS_ID}, {_SBD_ID}, {_EB_ID}
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i,
     generate_series(0, {OBS_PER_PROJECT - 1}) AS o,
     generate_series(0, {SBD_PER_OBS - 1}) AS s,
     generate_series(0, {EB_PER_SBD - 1}) AS e
ON CONFLICT (project_id, obs_id, sbd_id, eb_id) DO NOTHING
"""

# The wide one, and the only level that costs real time: it carries the
# footprint polygon, the spectral and temporal axes, and the instrument
# configuration. Products of one execution block cluster within half a degree
# of its pointing, so the sky looks like observations rather than confetti and
# a cone search returns a plausible group instead of one row from everywhere.
DATA_PRODUCTS = f"""
INSERT INTO srcnet.data_products (
    project_id, obs_id, sbd_id, eb_id, product_id,
    data_product_origin, o_ucd, dataproduct_type, calib_level, target_name,
    is_calibrator, calibrator_type, em_band,
    s_ra, s_dec, s_fov, s_region, s_region_geom,
    em_wlen, em_min, em_max, t_min, t_max, t_exptime,
    s_xel1, s_xel2, em_xel, t_xel,
    baseline_min, baseline_max, num_baselines, num_antennas,
    beam_size, beam_maj, beam_min, beam_pa, pol_states, pol_xel,
    baselines, calibrator_targets)
SELECT
    {_PROJECT_ID}, {_OBS_ID}, {_SBD_ID}, {_EB_ID},
    format('%%s-%%s', {_EB_ID}, lpad(p::text, 3, '0')),
    CASE WHEN p %% 4 = 0 THEN 'ADP' ELSE 'ODP' END,
    'phot.flux.density;em.radio',
    bench.pick(%(seed)s, ix, 'dptype', %(dataproduct_types)s),
    bench.rint(%(seed)s, ix, 'calib', 0, 3),
    bench.pick(%(seed)s, ix, 'target', %(targets)s),
    cal.is_cal,
    CASE WHEN cal.is_cal
         THEN bench.pick(%(seed)s, ix, 'caltype',
                         ARRAY['flux', 'bandpass', 'phase', 'polarization', 'delay'])
    END,
    bench.pick(%(seed)s, ix, 'band', %(bands)s),
    sky.ra, sky.dec, sky.fov,
    format('CIRCLE %%s %%s %%s',
           to_char(sky.ra, 'FM990.999999'), to_char(sky.dec, 'FM990.999999'),
           to_char(sky.fov / 2.0, 'FM990.999999')),
    bench.circle_spoly(sky.ra, sky.dec, sky.fov / 2.0),
    band.wlen, band.lo, band.hi,
    tim.t_min, tim.t_min + tim.exptime / 86400.0, tim.exptime,
    128 * bench.rint(%(seed)s, ix, 'sx1', 8, 32),
    128 * bench.rint(%(seed)s, ix, 'sx2', 8, 32),
    bench.rint(%(seed)s, ix, 'emx', 1, 4096),
    bench.rint(%(seed)s, ix, 'tx', 1, 120),
    bench.rnd(%(seed)s, ix, 'blmin') * 65.0 + 15.0,
    bench.rnd(%(seed)s, ix, 'blmax') * 149000.0 + 1000.0,
    ant.n * (ant.n - 1) / 2, ant.n,
    bench.rnd(%(seed)s, ix, 'beam') * 19.5 + 0.5,
    bench.rnd(%(seed)s, ix, 'bmaj') * 24.0 + 1.0,
    bench.rnd(%(seed)s, ix, 'bmin') * 19.5 + 0.5,
    bench.rnd(%(seed)s, ix, 'bpa') * 360.0 - 180.0,
    bench.pick(%(seed)s, ix, 'pol', ARRAY['/I/', '/I/Q/U/V/', '/XX/YY/', '/XX/XY/YX/YY/']),
    1 + 2 * bench.rint(%(seed)s, ix, 'polx', 0, 1),
    to_jsonb(ARRAY[
        round((bench.rnd(%(seed)s, ix, 'b0') * 149971.0 + 29.0)::numeric, 2),
        round((bench.rnd(%(seed)s, ix, 'b1') * 149971.0 + 29.0)::numeric, 2),
        round((bench.rnd(%(seed)s, ix, 'b2') * 149971.0 + 29.0)::numeric, 2),
        round((bench.rnd(%(seed)s, ix, 'b3') * 149971.0 + 29.0)::numeric, 2),
        round((bench.rnd(%(seed)s, ix, 'b4') * 149971.0 + 29.0)::numeric, 2),
        round((bench.rnd(%(seed)s, ix, 'b5') * 149971.0 + 29.0)::numeric, 2)]),
    CASE WHEN cal.is_cal
         THEN to_jsonb(ARRAY[bench.pick(%(seed)s, ix, 'caltgt', %(targets)s)])
         ELSE '[]'::jsonb END
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i,
     generate_series(0, {OBS_PER_PROJECT - 1}) AS o,
     generate_series(0, {SBD_PER_OBS - 1}) AS s,
     generate_series(0, {EB_PER_SBD - 1}) AS e,
     generate_series(0, {PRODUCTS_PER_EB - 1}) AS p,
     LATERAL (SELECT {_PRODUCT_IX} AS ix) AS ixs,
     LATERAL (SELECT (p %% 8) = 0 AS is_cal) AS cal,
     LATERAL (SELECT bench.rnd(%(seed)s, i, 'ra') * 360.0
                     + bench.rnd(%(seed)s, ix, 'dra') - 0.5 + 360.0 AS ra) AS raw,
     LATERAL (SELECT
        -- The project's pointing, then a degree-wide scatter per product, so
        -- a project reads as a survey field rather than as confetti.
        --
        -- Keyed on the project index with salts 'ra' and 'dec', which is
        -- exactly what corpus.object_position recomputes in Python: the cone
        -- searches aim at real fields by deriving the centre rather than by
        -- reading a row back, and that only works while the two agree.
        raw.ra - floor(raw.ra / 360.0) * 360.0 AS ra,
        greatest(-89.9, least(89.9,
            bench.sky_dec(%(seed)s, i) + bench.rnd(%(seed)s, ix, 'ddec') - 0.5)) AS dec,
        bench.rnd(%(seed)s, ix, 'fov') * 1.45 + 0.05 AS fov) AS sky,
     LATERAL (SELECT bench.rnd(%(seed)s, ix, 'wlen') * 1.15 + 0.05 AS wlen) AS w,
     LATERAL (SELECT w.wlen AS wlen,
        greatest(0.0195, w.wlen * (1.0 - bench.rnd(%(seed)s, ix, 'bw') * 0.4)) AS lo,
        least(6.0, w.wlen * (1.0 + bench.rnd(%(seed)s, ix, 'bw') * 0.4)) AS hi) AS band,
     LATERAL (SELECT
        %(mjd_lo)s + bench.rnd(%(seed)s, ix, 'tmin') * %(mjd_span)s AS t_min,
        bench.rnd(%(seed)s, ix, 'exp') * 21000.0 + 600.0 AS exptime) AS tim,
     LATERAL (SELECT bench.rint(%(seed)s, ix, 'ant', 64, 197) AS n) AS ant
ON CONFLICT (project_id, obs_id, sbd_id, eb_id, product_id) DO NOTHING
"""

# Artifacts carry access metadata and nothing spatial: that is how the ODP
# model's own example payload is shaped, and ivoa.obscore joins this table
# only for access_url/access_format/access_estsize. Giving each artifact a
# copy of its product's footprint would double the spoly construction — the
# most expensive thing in this file — for a column no query reads.
ARTIFACTS = f"""
INSERT INTO srcnet.artifacts (
    project_id, obs_id, sbd_id, eb_id, product_id, artifact_id,
    access_url, access_format, access_estsize, path_to_parent, semantics)
SELECT
    {_PROJECT_ID}, {_OBS_ID}, {_SBD_ID}, {_EB_ID},
    format('%%s-%%s', {_EB_ID}, lpad(p::text, 3, '0')),
    format('%%s-%%s-%%s', {_EB_ID}, lpad(p::text, 3, '0'), lpad(a::text, 2, '0')),
    format('https://data.srcnet.skao.int/%%s/%%s-%%s-%%s.%%s',
           {_PROJECT_ID}, {_EB_ID}, lpad(p::text, 3, '0'), lpad(a::text, 2, '0'),
           CASE WHEN a = 0 THEN 'fits' ELSE 'ms' END),
    CASE WHEN a = 0 THEN 'image/fits' ELSE 'application/x-casa-measurementset' END,
    (bench.rnd(%(seed)s, {_PRODUCT_IX} * 4 + a, 'size') * 7990000000.0 + 10000000.0)::bigint,
    format('./%%ss/', bench.pick(%(seed)s, {_PRODUCT_IX}, 'dptype', %(dataproduct_types)s)),
    CASE WHEN (p %% 8) = 0 THEN 'calibration'
         ELSE bench.pick(%(seed)s, {_PRODUCT_IX} * 4 + a, 'sem',
                         ARRAY['science', 'auxiliary', 'noise']) END
FROM generate_series(%(lo)s::bigint, %(hi)s::bigint) AS i,
     generate_series(0, {OBS_PER_PROJECT - 1}) AS o,
     generate_series(0, {SBD_PER_OBS - 1}) AS s,
     generate_series(0, {EB_PER_SBD - 1}) AS e,
     generate_series(0, {PRODUCTS_PER_EB - 1}) AS p,
     generate_series(0, {ARTIFACTS_PER_PRODUCT - 1}) AS a
ON CONFLICT (project_id, obs_id, sbd_id, eb_id, product_id, artifact_id) DO NOTHING
"""

# The software discovery model. Not size-driven: a catalogue of tools is
# hundreds of rows in reality and stays that way however large the data
# holdings get, so it is written once at the start of a build rather than
# grown. It exists because the service publishes two models under one query
# language, and a benchmark over only one of them would not exercise that.
SOFTWARE = """
INSERT INTO srcnet.software (
    uri, description, release_date, changelog, status,
    discovery_science_category, discovery_function_category,
    discovery_science_working_group, discovery_tools_included,
    data_compatibility_data_input_type, data_compatibility_data_output_type,
    resources_requires_gpu, resources_min_memory, resources_recommended_memory,
    provenance_repository_url, provenance_registered_by, provenance_registration_date)
SELECT
    format('%%s:%%s:%%s.%%s.%%s', pub.name, tool.name,
           1 + i %% 4, i %% 10, i %% 7),
    format('%%s, packaged by %%s for SRCNet', tool.name, pub.name),
    rel.at, format('https://gitlab.com/%%s/%%s/-/blob/CHANGELOG.md', pub.name, tool.name),
    bench.pick(%(seed)s, i, 'status',
               ARRAY['ALPHA', 'BETA', 'TESTING', 'STABLE', 'DEPRECATED']),
    to_jsonb(ARRAY[bench.pick(%(seed)s, i, 'swsci', %(science)s)]),
    to_jsonb(ARRAY[bench.pick(%(seed)s, i, 'swfun',
                              ARRAY['calibration', 'imaging', 'source-finding',
                                    'visualisation', 'simulation'])]),
    to_jsonb(ARRAY[bench.pick(%(seed)s, i, 'swwg', %(science)s)]),
    to_jsonb(ARRAY[tool.name]),
    to_jsonb(ARRAY[bench.pick(%(seed)s, i, 'swin',
                              ARRAY['visibility', 'image', 'cube', 'catalogue'])]),
    to_jsonb(ARRAY[bench.pick(%(seed)s, i, 'swout',
                              ARRAY['visibility', 'image', 'cube', 'catalogue'])]),
    (i %% 5) = 0,
    (1 + bench.rint(%(seed)s, i, 'swmem', 1, 7)) * 1073741824::bigint,
    (8 + bench.rint(%(seed)s, i, 'swrec', 8, 56)) * 1073741824::bigint,
    format('https://gitlab.com/%%s/%%s', pub.name, tool.name),
    format('%%s-ci', pub.name),
    rel.at
FROM generate_series(0::bigint, %(software)s::bigint - 1) AS i,
     LATERAL (SELECT bench.pick(%(seed)s, i, 'pub',
                     ARRAY['skao', 'srcnet', 'cadc', 'astron', 'inaf']) AS name) AS pub,
     LATERAL (SELECT bench.pick(%(seed)s, i, 'tool',
                     ARRAY['rascil', 'casa', 'wsclean', 'carta', 'aoflagger', 'sofia',
                           'dask-ms', 'katdal', 'oskar', 'casacore', 'topcat']) AS name) AS tool,
     LATERAL (SELECT to_timestamp(1672531200 + bench.rint(%(seed)s, i, 'rel', 0, 900) * 86400)
                     AS at) AS rel
ON CONFLICT (uri) DO NOTHING
"""

SOFTWARE_ARTIFACTS = """
INSERT INTO srcnet.software_artifacts (
    uri, kind, location, cpu_architecture, digest, entrypoint, supported_modes)
SELECT
    s.uri,
    k.kind,
    format('registry.gitlab.com/%%s:%%s', split_part(s.uri, ':', 2), lower(k.kind)),
    CASE WHEN k.n = 0 THEN '["x86_64"]'::jsonb ELSE '["x86_64", "aarch64"]'::jsonb END,
    format('sha256:%%s%%s', md5(s.uri || k.kind), md5(k.kind || s.uri)),
    format('/opt/%%s/bin/%%s', split_part(s.uri, ':', 2), split_part(s.uri, ':', 2)),
    '["batch", "interactive"]'::jsonb
FROM srcnet.software AS s,
     LATERAL (SELECT n, (ARRAY['DOCKER', 'SINGULARITY', 'OCI'])[n + 1] AS kind
                FROM generate_series(0, 1) AS n) AS k
ON CONFLICT (uri, location) DO NOTHING
"""

# Parents first: every level carries a foreign key to the one above it.
LEVELS = (
    ("project", PROJECTS),
    ("observation", OBSERVATIONS),
    ("scheduling_block", SCHEDULING_BLOCKS),
    ("execution_block", EXECUTION_BLOCKS),
    ("data_product", DATA_PRODUCTS),
    ("artifact", ARTIFACTS),
)

TABLES = (
    "srcnet.projects",
    "srcnet.observations",
    "srcnet.scheduling_blocks",
    "srcnet.execution_blocks",
    "srcnet.data_products",
    "srcnet.artifacts",
    "srcnet.software",
    "srcnet.software_artifacts",
)

# The spatial indexes belong to the service, which creates them from the ODP
# model at bootstrap. The suite drops them for the duration of a load and
# rebuilds them once at the end: a GiST index maintained row by row through
# tens of millions of inserts is the single slowest thing in this file, and
# rebuilding each one from the finished table is minutes.
#
# Read from pg_indexes rather than written out here, so the suite rebuilds
# whatever the service actually declared rather than a stale copy of it.
SPATIAL_INDEX_QUERY = """
SELECT indexname, indexdef FROM pg_indexes
 WHERE schemaname = 'srcnet' AND indexdef LIKE '%USING gist%'
 ORDER BY indexname
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
        "instruments": gen["instruments"],
        "dataproduct_types": gen["dataproduct_types"],
        "bands": gen["bands"],
        "science": gen["science_categories"],
        "targets": gen["targets"],
        "surnames": gen["surnames"],
        "software": gen["software_packages"],
        "mjd_lo": mjd_lo,
        "mjd_span": mjd_hi - mjd_lo,
    }


def database_bytes(conn) -> int:
    return conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]


def highest_project(conn) -> int:
    """Where a previous, interrupted generation got to.

    Read off the *deepest* level, not the first one. Levels are written parent
    first and committed one at a time, so a run that dies between them leaves
    projects whose children were never generated. Resuming from the highest
    project id would step straight over those and leave a block of the corpus
    permanently empty — which is not a visible failure, just a sky with holes
    in it and cone searches that mysteriously find nothing.

    Taking the maximum from srcnet.artifacts means the resume point is the last
    project that made it all the way down, and the partial ones are generated
    again. That is safe because every statement is ON CONFLICT DO NOTHING.

    The index is read off the identifier rather than a sequence column: every
    level derives its keys from the project index, so the index is what a
    resume needs and the ODP model has no synthetic counter to carry it.
    """
    row = conn.execute(
        """
        SELECT coalesce(
            -- the project before the first one whose children are missing
            (SELECT min(substring(p.project_id from 7)::bigint) - 1
               FROM srcnet.projects AS p
              WHERE NOT EXISTS (SELECT 1 FROM srcnet.artifacts AS a
                                 WHERE a.project_id = p.project_id)),
            -- nothing incomplete: carry on from the deepest level written
            (SELECT max(substring(project_id from 7)::bigint) FROM srcnet.artifacts),
            0)
        """
    ).fetchone()
    return int(row[0] or 0)


def apply_schema(conn) -> None:
    """Load the suite's additions on top of the service's own schema.

    The srcnet tables and the ivoa.obscore view are created by the metadata
    plugins at API bootstrap, so this waits for them rather than defining its
    own. Nothing here drops or replaces the view: the suite now generates the
    same ODP model the service publishes, so the two no longer collide over
    the ObsCore name — which is what used to make TAP_SCHEMA advertise columns
    the relation did not have.
    """
    missing = conn.execute(
        "SELECT to_regclass('srcnet.data_products') IS NULL"
        "    OR to_regclass('ivoa.obscore') IS NULL"
    ).fetchone()[0]
    if missing:
        raise RuntimeError(
            "srcnet.data_products or ivoa.obscore is absent: the ODP metadata plugin "
            "creates them at API bootstrap, so deploy the service against this "
            "database before generating a dataset"
        )
    for name in ("generate.sql", "schema.sql"):
        conn.execute((HERE / name).read_text())
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
        # one data product is one ObsCore row: the view is 1:1 over them,
        # and counting it through the view would re-run the join for a
        # number the base table already has
        obscore_rows=rows["srcnet.data_products"],
        index_to_table_ratio=sum(index_bytes.values()) / total_table,
        observations_generated=observations,
        seconds=seconds,
    )


def grow_to(conn, cfg: dict, target_bytes: int, batch_projects: int) -> int:
    """Generate until the database reaches target_bytes. Returns the highest
    project index written."""
    index = highest_project(conn)
    if index:
        log.info("resuming generation from project %d", index)
    while True:
        size = database_bytes(conn)
        if size >= target_bytes:
            return index
        log.info(
            "%.2f/%.2f GiB (%.1f%%), %d projects",
            size / 2**30,
            target_bytes / 2**30,
            100.0 * size / target_bytes,
            index,
        )
        lo, hi = index + 1, index + batch_projects
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


@contextlib.contextmanager
def _spatial_indexes_set_aside(conn):
    """Drop the service's GiST indexes for the load, rebuild them after.

    Measured while loading the demo corpus: with the indexes live the load ran
    at roughly a third of the rate, because every spoly insert pays a GiST
    descent and a page split one row at a time. Building each index once from
    the finished table is minutes.

    The dropped definitions are stashed in the database, in the same
    transaction as the DROP, rather than only in this process's memory. A
    finally covers an exception but not a kill, and the failure that leaves
    behind is the quiet one: the indexes stay dropped, the next run finds
    nothing to set aside, and every spatial query silently sequential-scans.
    Stashed, the next run puts them back.
    """
    conn.execute(f"CREATE TABLE IF NOT EXISTS {STASH} (name text PRIMARY KEY, ddl text NOT NULL)")
    live = conn.execute(SPATIAL_INDEX_QUERY).fetchall()
    for name, ddl in live:
        conn.execute(
            f"INSERT INTO {STASH} (name, ddl) VALUES (%s, %s) ON CONFLICT DO NOTHING", (name, ddl)
        )
        conn.execute(f"DROP INDEX IF EXISTS srcnet.{name}")
    conn.commit()
    saved = conn.execute(f"SELECT name, ddl FROM {STASH} ORDER BY name").fetchall()
    log.info("set aside %d GiST index(es) for the load", len(saved))
    try:
        yield
    finally:
        # Roll back first: if the load failed mid-statement the transaction is
        # aborted, and every rebuild would then fail with "current transaction
        # is aborted" — burying the error that actually mattered under one
        # that does not.
        conn.rollback()
        started = time.monotonic()
        for _, ddl in saved:
            conn.execute(ddl)
        conn.execute(f"DROP TABLE IF EXISTS {STASH}")
        conn.commit()
        log.info("rebuilt %d GiST index(es) in %.0fs", len(saved), time.monotonic() - started)


def restore_stashed_indexes(conn) -> int:
    """Rebuild any GiST indexes an earlier run left set aside. Returns how many.

    The stash is only drained by `_spatial_indexes_set_aside` on its way out,
    so a caller that decides not to set the indexes aside — because the target
    is already met, or because the load is small enough to keep them live — has
    to drain it itself. Without this a killed run leaves the indexes dropped
    and every later run skips past them, which is the quiet failure the stash
    exists to prevent: spatial queries keep answering, by sequential scan, for
    ever.
    """
    present = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (STASH,)).fetchone()[0]
    if not present:
        return 0
    saved = conn.execute(f"SELECT name, ddl FROM {STASH} ORDER BY name").fetchall()
    if not saved:
        conn.execute(f"DROP TABLE IF EXISTS {STASH}")
        conn.commit()
        return 0
    log.info("restoring %d GiST index(es) left set aside by an earlier run", len(saved))
    started = time.monotonic()
    for _, ddl in saved:
        conn.execute(ddl)
    conn.execute(f"DROP TABLE IF EXISTS {STASH}")
    conn.commit()
    log.info("restored %d GiST index(es) in %.0fs", len(saved), time.monotonic() - started)
    return len(saved)


def build(dsn: str, cfg: dict, targets: list[dict], out_dir: pathlib.Path) -> list[DatasetStats]:
    """Grow through every target, checkpointing stats at each."""
    out_dir.mkdir(parents=True, exist_ok=True)
    built = []
    with psycopg.connect(dsn, autocommit=False) as conn:
        apply_schema(conn)
        # The software catalogue is a fixed few hundred rows whatever the tier,
        # so it is written once rather than grown.
        params = _params(cfg, 0, 0)
        for statement in (SOFTWARE, SOFTWARE_ARTIFACTS):
            with conn.cursor() as cur:
                cur.execute(statement, params)
        conn.commit()
        for target in targets:
            marker = out_dir / f"{target['name']}.json"
            if marker.exists():
                log.info("%s already built, skipping", target["name"])
                built.append(DatasetStats(**json.loads(marker.read_text())))
                continue
            started = time.monotonic()
            with _spatial_indexes_set_aside(conn):
                projects = grow_to(
                    conn, cfg, target["target_bytes"], cfg["generation"]["batch_projects"]
                )
            log.info("VACUUM ANALYZE for %s", target["name"])
            with psycopg.connect(dsn, autocommit=True) as vac:
                vac.execute("VACUUM ANALYZE")
            stats = collect_stats(conn, target["name"], projects, time.monotonic() - started)
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
