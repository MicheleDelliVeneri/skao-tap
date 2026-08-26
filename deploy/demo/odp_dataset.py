"""The demo's dataset: the ODP and software data models, at scale.

The benchmark suite's generator builds a synthetic CAOM hierarchy and
*replaces* ivoa.obscore with a table of its own. That is the right shape for
measuring query throughput against a fixed corpus, and the wrong one for a
demo: it puts a data model this service does not implement in front of the
audience, leaves every srcnet table empty, and turns the ObsCore view into a
table so the mapping being demonstrated is not the one running.

This fills the models the service actually implements — the ODP hierarchy
(projects > observations > scheduling blocks > execution blocks > data
products > artifacts) and the software discovery model — and leaves
ivoa.obscore as the plugin's view over them, which is the thing worth
showing.

Rows are generated here and COPY'd rather than built by INSERT ... SELECT
generate_series in SQL, because s_region_geom has to come from
``stcs_to_spoly``. That routine is tuned around pgsphere's coordinate epsilon
and the degenerate-polygon case; a second copy of it in SQL would be a second
thing to keep correct, and the demo would be showing geometry the ingest path
never produces. COPY costs a data-transfer round trip and still moves ~100k
rows/s, which is well inside what this needs.

Deterministic: same seed, same rows, so a re-run after a failure resumes to
the same corpus rather than a differently-random one.
"""

import argparse
import contextlib
import datetime as dt
import json
import math
import random
import sys
import time

import psycopg
from egernia_core.metadata.regions import stcs_to_spoly

# One project's fan-out. Chosen so a project is a plausible observing
# programme rather than to hit a row count: 4 observations, each split into
# two scheduling blocks, each executed twice, each execution yielding eight
# data products with two artifacts apiece.
OBS_PER_PROJECT = 4
SBD_PER_OBS = 2
EB_PER_SBD = 2
PRODUCTS_PER_EB = 8
ARTIFACTS_PER_PRODUCT = 2
PRODUCTS_PER_PROJECT = OBS_PER_PROJECT * SBD_PER_OBS * EB_PER_SBD * PRODUCTS_PER_EB

DATAPRODUCT_TYPES = ("image", "cube", "spectrum", "visibility", "sed", "timeseries", "table")
CALIBRATOR_TYPES = ("flux", "bandpass", "phase", "polarization", "delay")
SEMANTICS = ("science", "auxiliary", "noise", "calibration")
DATA_RIGHTS = ("public", "proprietary", "private")
INSTRUMENTS = ("SKA-Mid", "SKA-Low")
EM_BANDS = ("Radio", "Millimeter")
POL_STATES = ("/I/", "/I/Q/U/V/", "/XX/YY/", "/XX/XY/YX/YY/")
SOFTWARE_STATUS = ("ALPHA", "BETA", "TESTING", "STABLE", "DEPRECATED")
ARTIFACT_KINDS = ("DOCKER", "SINGULARITY", "OCI")

TARGETS = (
    "Abell 2744",
    "NGC 253",
    "Centaurus A",
    "Fornax A",
    "Vela X-1",
    "LMC",
    "SMC",
    "Sagittarius A*",
    "M83",
    "Circinus",
    "Pictor A",
    "Hydra A",
    "PKS 1934-638",
)
SCIENCE_CATEGORIES = (
    "Cosmology",
    "Cradle of Life",
    "Epoch of Reionisation",
    "Extragalactic Continuum",
    "HI Galaxy Science",
    "Magnetism",
    "Pulsars",
    "Transients",
)
FUNCTION_CATEGORIES = ("calibration", "imaging", "source-finding", "visualisation", "simulation")
INPUT_TYPES = ("visibility", "image", "cube", "catalogue")

# The MJD window the synthetic observations fall in: 2025-01-01 onwards, so
# t_min/t_max read as plausible recent dates in a client that renders them.
MJD_START = 60676.0
MJD_SPAN = 730.0

# Where a load parks the index definitions it dropped, so a run that is killed
# rather than raised can still be recovered from by the next one.
STASH = "srcnet._demo_stashed_indexes"

TABLES = (
    "srcnet.artifacts",
    "srcnet.data_products",
    "srcnet.execution_blocks",
    "srcnet.scheduling_blocks",
    "srcnet.observations",
    "srcnet.projects",
    "srcnet.software_artifacts",
    "srcnet.software",
)

PROJECT_COLUMNS = (
    "schema_version",
    "project_id",
    "group_ids",
    "project_title",
    "pi_name",
    "data_rights",
)
OBSERVATION_COLUMNS = (
    "project_id",
    "obs_id",
    "obs_title",
    "collection",
    "instrument_name",
    "facility_name",
)
SBD_COLUMNS = ("project_id", "obs_id", "sbd_id")
EB_COLUMNS = ("project_id", "obs_id", "sbd_id", "eb_id")
PRODUCT_COLUMNS = (
    "project_id",
    "obs_id",
    "sbd_id",
    "eb_id",
    "product_id",
    "data_product_origin",
    "o_ucd",
    "dataproduct_type",
    "calib_level",
    "target_name",
    "is_calibrator",
    "calibrator_type",
    "em_band",
    "s_ra",
    "s_dec",
    "s_fov",
    "s_region",
    "s_region_geom",
    "em_wlen",
    "em_min",
    "em_max",
    "t_min",
    "t_max",
    "t_exptime",
    "s_xel1",
    "s_xel2",
    "em_xel",
    "t_xel",
    "baseline_min",
    "baseline_max",
    "num_baselines",
    "num_antennas",
    "beam_size",
    "beam_maj",
    "beam_min",
    "beam_pa",
    "pol_states",
    "pol_xel",
    "baselines",
    "calibrator_targets",
)
ARTIFACT_COLUMNS = (
    "project_id",
    "obs_id",
    "sbd_id",
    "eb_id",
    "product_id",
    "artifact_id",
    "access_url",
    "access_format",
    "access_estsize",
    "path_to_parent",
    "semantics",
    "s_ra",
    "s_dec",
    "s_fov",
    "s_region",
    "s_region_geom",
    "em_wlen",
    "em_min",
    "em_max",
    "t_min",
    "t_max",
    "t_exptime",
    "pol_states",
    "pol_xel",
)
SOFTWARE_COLUMNS = (
    "uri",
    "description",
    "release_date",
    "changelog",
    "status",
    "discovery_science_category",
    "discovery_function_category",
    "discovery_science_working_group",
    "discovery_tools_included",
    "data_compatibility_data_input_type",
    "data_compatibility_data_output_type",
    "resources_requires_gpu",
    "resources_min_memory",
    "resources_recommended_memory",
    "provenance_repository_url",
    "provenance_registered_by",
    "provenance_registration_date",
)
SOFTWARE_ARTIFACT_COLUMNS = (
    "uri",
    "kind",
    "location",
    "cpu_architecture",
    "digest",
    "entrypoint",
    "supported_modes",
)


def _sky(rng: random.Random) -> tuple[float, float]:
    """A position drawn uniformly over the sky SKA can see.

    ``asin`` of a uniform draw rather than a uniform declination: the latter
    piles sources up at the poles, which shows as a visibly wrong density map
    the moment anyone plots the corpus.
    """
    ra = rng.uniform(0.0, 360.0)
    dec = math.degrees(
        math.asin(rng.uniform(math.sin(math.radians(-85.0)), math.sin(math.radians(35.0))))
    )
    return ra, dec


def _band(rng: random.Random) -> tuple[float, float, float]:
    """(em_wlen, em_min, em_max) inside the model's 0.0195-6.0 m bounds."""
    wlen = rng.uniform(0.05, 1.2)
    half = wlen * rng.uniform(0.05, 0.4)
    return wlen, max(0.0195, wlen - half), min(6.0, wlen + half)


def _project_rows(index: int, rng: random.Random):
    """Every row one project contributes, as (table, row) pairs."""
    project_id = f"SKAO-P{index:07d}"
    rights = DATA_RIGHTS[index % len(DATA_RIGHTS)]
    yield (
        "projects",
        (
            "2.1",
            project_id,
            json.dumps([f"{project_id}_group"]),
            f"{rng.choice(SCIENCE_CATEGORIES)} programme {index}",
            f"Dr. {rng.choice(('Smith', 'Okafor', 'Nakamura', 'Rossi', 'Dlamini', 'Moreau'))}",
            rights,
        ),
    )
    for o in range(OBS_PER_PROJECT):
        instrument = INSTRUMENTS[(index + o) % len(INSTRUMENTS)]
        obs_id = f"{instrument}-{index:07d}-{o:02d}"
        target = rng.choice(TARGETS)
        yield (
            "observations",
            (
                project_id,
                obs_id,
                f"{rng.choice(SCIENCE_CATEGORIES)} observation of {target}",
                f"SKAO/{instrument}",
                instrument,
                "Square Kilometre Array Observatory",
            ),
        )
        # one pointing per observation, so its products cluster on the sky the
        # way a real observation's do rather than scattering globally
        centre_ra, centre_dec = _sky(rng)
        for s in range(SBD_PER_OBS):
            sbd_id = f"sbd-{index:07d}-{o:02d}-{s:02d}"
            yield "scheduling_blocks", (project_id, obs_id, sbd_id)
            for e in range(EB_PER_SBD):
                eb_id = f"eb-{index:07d}-{o:02d}-{s:02d}-{e:03d}"
                yield "execution_blocks", (project_id, obs_id, sbd_id, eb_id)
                t_min = MJD_START + rng.uniform(0.0, MJD_SPAN)
                exptime = rng.uniform(600.0, 21600.0)
                for p in range(PRODUCTS_PER_EB):
                    yield from _product_rows(
                        rng,
                        project_id,
                        obs_id,
                        sbd_id,
                        eb_id,
                        p,
                        centre_ra,
                        centre_dec,
                        t_min,
                        exptime,
                    )


def _product_rows(rng, project_id, obs_id, sbd_id, eb_id, p, centre_ra, centre_dec, t_min, exptime):
    product_id = f"{eb_id}-{p:03d}"
    dp_type = DATAPRODUCT_TYPES[p % len(DATAPRODUCT_TYPES)]
    # products of one execution block sit within a degree of its pointing
    ra = (centre_ra + rng.uniform(-0.5, 0.5)) % 360.0
    dec = max(-89.9, min(89.9, centre_dec + rng.uniform(-0.5, 0.5)))
    fov = rng.uniform(0.05, 1.5)
    region = f"CIRCLE {ra:.6f} {dec:.6f} {fov / 2:.6f}"
    geom = stcs_to_spoly(region)
    wlen, em_min, em_max = _band(rng)
    t_max = t_min + exptime / 86400.0
    is_calibrator = p % 8 == 0
    antennas = rng.randint(64, 197)
    yield (
        "data_products",
        (
            project_id,
            obs_id,
            sbd_id,
            eb_id,
            product_id,
            "ODP" if p % 4 else "ADP",
            "phot.flux.density;em.radio",
            dp_type,
            min(3, 1 + p % 3),
            rng.choice(TARGETS),
            is_calibrator,
            rng.choice(CALIBRATOR_TYPES) if is_calibrator else None,
            rng.choice(EM_BANDS),
            ra,
            dec,
            fov,
            region,
            geom,
            wlen,
            em_min,
            em_max,
            t_min,
            t_max,
            exptime,
            rng.choice((1024, 2048, 4096)),
            rng.choice((1024, 2048, 4096)),
            rng.choice((1, 64, 4096)),
            rng.randint(1, 120),
            rng.uniform(15.0, 80.0),
            rng.uniform(1000.0, 150000.0),
            antennas * (antennas - 1) // 2,
            antennas,
            rng.uniform(0.5, 20.0),
            rng.uniform(1.0, 25.0),
            rng.uniform(0.5, 20.0),
            rng.uniform(-180.0, 180.0),
            rng.choice(POL_STATES),
            rng.choice((1, 2, 4)),
            json.dumps([round(rng.uniform(29.0, 150000.0), 2) for _ in range(6)]),
            json.dumps([rng.choice(TARGETS)] if is_calibrator else []),
        ),
    )
    for a in range(ARTIFACTS_PER_PRODUCT):
        suffix = "fits" if a == 0 else "ms"
        yield (
            "artifacts",
            (
                project_id,
                obs_id,
                sbd_id,
                eb_id,
                product_id,
                f"{product_id}-{a:02d}",
                f"https://data.srcnet.skao.int/{project_id}/{obs_id}/{product_id}-{a:02d}.{suffix}",
                "image/fits" if a == 0 else "application/x-casa-measurementset",
                rng.randint(10_000_000, 8_000_000_000),
                f"./{dp_type}s/",
                SEMANTICS[a % len(SEMANTICS)] if not is_calibrator else "calibration",
                # An artifact's spatial, spectral and temporal columns are left
                # unset, as they are in the model's own example payload: an
                # artifact is a file belonging to a data product, and the
                # product is where the footprint lives. ivoa.obscore agrees —
                # it joins artifacts only for access_url/format/estsize.
                #
                # Also the difference between a 20-minute load and an hour.
                # Postgres was CPU-bound parsing 32-vertex spoly literals, and
                # two thirds of them were these duplicates of the product's
                # own footprint.
                *(None,) * 13,
            ),
        )


def _software_rows(count: int, rng: random.Random):
    """The software discovery model: packages and their container artifacts."""
    publishers = ("skao", "srcnet", "cadc", "astron", "inaf")
    names = (
        "rascil",
        "casa",
        "wsclean",
        "carta",
        "aoflagger",
        "sofia",
        "cubelets",
        "dask-ms",
        "katdal",
        "oskar",
        "pyuvdata",
        "casacore",
        "montage",
        "topcat",
        "ds9",
    )
    for i in range(count):
        publisher = publishers[i % len(publishers)]
        name = names[i % len(names)]
        version = f"{1 + i % 4}.{i % 10}.{i % 7}"
        uri = f"{publisher}:{name}:{version}"
        released = dt.datetime(2023, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=rng.randint(0, 900))
        yield (
            "software",
            (
                uri,
                f"{name} {version}, packaged by {publisher} for SRCNet",
                released,
                f"https://gitlab.com/{publisher}/{name}/-/blob/{version}/CHANGELOG.md",
                SOFTWARE_STATUS[i % len(SOFTWARE_STATUS)],
                json.dumps(rng.sample(SCIENCE_CATEGORIES, k=2)),
                json.dumps(rng.sample(FUNCTION_CATEGORIES, k=2)),
                json.dumps(rng.sample(SCIENCE_CATEGORIES, k=1)),
                json.dumps([name, f"{name}-cli"]),
                json.dumps(rng.sample(INPUT_TYPES, k=2)),
                json.dumps(rng.sample(INPUT_TYPES, k=2)),
                i % 5 == 0,
                rng.choice((2, 4, 8)) * 1024**3,
                rng.choice((16, 32, 64)) * 1024**3,
                f"https://gitlab.com/{publisher}/{name}",
                f"{publisher}-ci",
                released,
            ),
        )
        for a, kind in enumerate(ARTIFACT_KINDS[: 1 + i % len(ARTIFACT_KINDS)]):
            yield (
                "software_artifacts",
                (
                    uri,
                    kind,
                    f"registry.gitlab.com/{publisher}/{name}:{version}-{kind.lower()}",
                    json.dumps(["x86_64"] if a % 2 == 0 else ["x86_64", "aarch64"]),
                    f"sha256:{rng.getrandbits(256):064x}",
                    f"/opt/{name}/bin/{name}",
                    json.dumps(rng.sample(("batch", "interactive", "notebook"), k=2)),
                ),
            )


COLUMNS = {
    "projects": PROJECT_COLUMNS,
    "observations": OBSERVATION_COLUMNS,
    "scheduling_blocks": SBD_COLUMNS,
    "execution_blocks": EB_COLUMNS,
    "data_products": PRODUCT_COLUMNS,
    "artifacts": ARTIFACT_COLUMNS,
    "software": SOFTWARE_COLUMNS,
    "software_artifacts": SOFTWARE_ARTIFACT_COLUMNS,
}
# parents first: every child carries a foreign key to the level above
ORDER = (
    "projects",
    "observations",
    "scheduling_blocks",
    "execution_blocks",
    "data_products",
    "artifacts",
    "software",
    "software_artifacts",
)


def _flush(conn, buffers: dict) -> None:
    """COPY each table's buffered rows, parents first."""
    for table in ORDER:
        rows = buffers.get(table)
        if not rows:
            continue
        columns = ", ".join(COLUMNS[table])
        with conn.cursor().copy(f"COPY srcnet.{table} ({columns}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
        rows.clear()


def clear(conn) -> None:
    """Empty the ODP and software tables. TRUNCATE rather than DELETE: this
    reloads a demo corpus, and the cascade order is already in the FKs."""
    conn.execute(f"TRUNCATE {', '.join(TABLES)} CASCADE")


def _spatial_indexes(conn) -> list[tuple[str, str]]:
    """(name, DDL) for the GiST indexes on the tables about to be loaded.

    Read from pg_indexes rather than written out here: the service owns these
    definitions, and a hand-copied duplicate would silently rebuild yesterday's
    index the first time the service changed one.
    """
    return conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes"
        " WHERE schemaname = 'srcnet' AND indexdef LIKE '%USING gist%'"
        " ORDER BY indexname"
    ).fetchall()


@contextlib.contextmanager
def _indexes_set_aside(conn):
    """Drop the GiST indexes for the load and rebuild them once at the end.

    Measured on this corpus: with the indexes live the load ran at ~40
    projects/minute, where pure row generation accounts for 1.2 minutes of the
    whole run. Every spoly insert was paying GiST descent and a page split one
    row at a time; building each index once, from the finished table, is most
    of the difference between twenty minutes and two hours.

    The dropped definitions are stashed *in the database*, in the same
    transaction as the DROP, rather than only in this process's memory. A
    ``finally`` covers an exception but not a kill -9, and the failure that
    leaves behind is the quiet one: the indexes stay dropped, the next run
    finds nothing to set aside, and the demo ends up spatially seq-scanning
    with no error anywhere to say why. Stashed, a later run puts them back.
    """
    conn.execute(f"CREATE TABLE IF NOT EXISTS {STASH} (name text PRIMARY KEY, ddl text NOT NULL)")
    orphaned = conn.execute(f"SELECT name, ddl FROM {STASH}").fetchall()
    if orphaned:
        print(f"  found {len(orphaned)} index(es) a previous run left dropped", flush=True)
    live = _spatial_indexes(conn)
    for name, ddl in live:
        conn.execute(
            f"INSERT INTO {STASH} (name, ddl) VALUES (%s, %s) ON CONFLICT DO NOTHING", (name, ddl)
        )
        conn.execute(f"DROP INDEX IF EXISTS srcnet.{name}")
    conn.commit()
    saved = conn.execute(f"SELECT name, ddl FROM {STASH} ORDER BY name").fetchall()
    print(f"  set aside {len(saved)} GiST index(es) for the load", flush=True)
    try:
        yield
    finally:
        started = time.monotonic()
        for _, ddl in saved:
            conn.execute(ddl)
        conn.execute(f"DROP TABLE IF EXISTS {STASH}")
        conn.commit()
        print(
            f"  rebuilt {len(saved)} GiST index(es) in {time.monotonic() - started:.0f}s",
            flush=True,
        )


def build(
    dsn: str, projects: int, software: int, *, seed: int = 20260826, batch: int = 200
) -> dict:
    """Fill the ODP and software tables. Returns the row count per table."""
    started = time.monotonic()
    rng = random.Random(seed)
    buffers: dict[str, list] = {name: [] for name in ORDER}
    written = dict.fromkeys(ORDER, 0)
    with psycopg.connect(dsn) as conn, _indexes_set_aside(conn):
        for index in range(projects):
            for table, row in _project_rows(index, rng):
                buffers[table].append(row)
                written[table] += 1
            if (index + 1) % batch == 0:
                _flush(conn, buffers)
                conn.commit()
                done = index + 1
                print(
                    f"  {done}/{projects} projects  "
                    f"{written['data_products']} products  "
                    f"{time.monotonic() - started:.0f}s",
                    flush=True,
                )
        for table, row in _software_rows(software, rng):
            buffers[table].append(row)
            written[table] += 1
        _flush(conn, buffers)
        conn.commit()
        # the planner has no statistics for rows that arrived by COPY, and a
        # demo whose first spatial query seq-scans is a demo that looks slow
        conn.execute("ANALYZE")
        conn.commit()
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True)
    parser.add_argument(
        "--projects",
        type=int,
        default=4700,
        help=f"projects to generate; each contributes {PRODUCTS_PER_PROJECT} data products",
    )
    parser.add_argument("--software", type=int, default=400)
    parser.add_argument("--seed", type=int, default=20260826)
    parser.add_argument("--truncate", action="store_true", help="empty the tables first")
    args = parser.parse_args(argv)

    if args.truncate:
        with psycopg.connect(args.dsn) as conn:
            clear(conn)
            conn.commit()
        print("truncated the ODP and software tables")

    print(
        f"generating {args.projects} projects "
        f"(~{args.projects * PRODUCTS_PER_PROJECT} data products) "
        f"and {args.software} software packages"
    )
    written = build(args.dsn, args.projects, args.software, seed=args.seed)
    print("\nrows written:")
    for table in ORDER:
        print(f"  srcnet.{table:<20} {written[table]:>10,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
