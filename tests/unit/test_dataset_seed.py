"""The dataset seeder's arithmetic, which two deployments depend on.

Unit tests: no database, no cluster. What they pin is the two facts a caller
gets wrong — that the ODP fan-out fixes the ratio between a data-product count
and everything else, and that the software catalogue has a hard ceiling no
amount of asking will exceed.
"""

import math
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2] / "dataset"
sys.path.insert(0, str(ROOT))

from egernia_dataset import generate, seed  # noqa: E402


def test_seeding_is_row_driven_and_respects_the_fan_out():
    """A seeded deployment is described by rows, not by a benchmark tier.

    The suite's generation is size-driven because a benchmark cares what N GiB
    does to a query plan. Someone asking for a populated environment states a
    row count instead, and the model's fixed fan-out turns that into projects:
    128 data products per project, so equal row counts across the ODP tables
    are not available — the ratios are the hierarchy.
    """
    assert seed.PRODUCTS_PER_PROJECT == 128

    # Rounded up, so the target is reached rather than approached.
    assert seed.projects_for(500_000) == 3907
    assert seed.projects_for(128) == 1
    assert seed.projects_for(129) == 2

    # 3907 projects overshoot 500,000 slightly, which is the point of ceil.
    assert seed.projects_for(500_000) * seed.PRODUCTS_PER_PROJECT >= 500_000


def test_the_software_catalogue_has_a_ceiling_the_seeder_knows_about():
    """srcnet.software cannot be grown to an arbitrary size.

    Its uri is {publisher}:{tool}:{major}.{minor}.{patch} over 5 publishers,
    11 tools and a version triple taken from the row index modulo 4, 10 and 7.
    That is 5 * 11 * lcm(4, 10, 7) = 7,700 distinct uris, and ON CONFLICT
    (uri) DO NOTHING discards everything past them — so asking for 500,000
    quietly yields 7,700. The seeder warns rather than misreporting, and this
    pins the arithmetic to the statement that produces it.
    """
    publishers = re.search(r"'pub',\s*\n?\s*ARRAY\[([^\]]+)\]", generate.SOFTWARE)
    tools = re.search(r"'tool',\s*\n?\s*ARRAY\[([^\]]+)\]", generate.SOFTWARE)
    assert publishers and tools, "the software uri vocabulary moved; re-derive the ceiling"
    n_pub = publishers.group(1).count("'") // 2
    n_tool = tools.group(1).count("'") // 2

    # The version triple in the statement: 1 + i %% 4, i %% 10, i %% 7.
    moduli = [int(m) for m in re.findall(r"i %% (\d+)", generate.SOFTWARE)[:3]]
    assert moduli == [4, 10, 7], "the version arithmetic moved; re-derive the ceiling"

    assert n_pub * n_tool * math.lcm(*moduli) == seed.SOFTWARE_URI_CEILING


def test_every_path_that_skips_the_set_aside_drains_the_stash():
    """A killed rebuild must not leave the spatial indexes dropped for ever.

    `_spatial_indexes_set_aside` drops the GiST footprint indexes, stashes
    their definitions in the database so a kill cannot lose them, and rebuilds
    them on the way out. Only on the way out — so a run that decides not to set
    them aside, because the target is already met or the load is small enough
    to keep them live, has to drain the stash itself.

    Getting this wrong is silent: spatial queries keep answering, by sequential
    scan over every row, and nothing fails. Found by inspection while adding
    those two paths, with a deployment sitting in exactly that state — four
    definitions stashed, no GiST index present.
    """
    import inspect

    source = inspect.getsource(seed.main)
    # The two branches that never enter the context manager.
    early_return, live_indexes = (
        source.split("elif target_products > SETASIDE_INDEXES_ABOVE:")[0],
        source.split("else:")[-1],
    )
    for name, branch in (("already-seeded", early_return), ("indexes-live", live_indexes)):
        assert "restore_stashed_indexes" in branch, (
            f"the {name} path skips _spatial_indexes_set_aside without draining the stash"
        )
    assert callable(generate.restore_stashed_indexes)
