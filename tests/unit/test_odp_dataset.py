"""The demo's ODP dataset generator.

Rows are COPY'd positionally, so a value list that drifts out of step with its
column list does not fail loudly — it writes every subsequent value into the
wrong column, and the first sign is a demo where s_fov holds a wavelength.
That is the one thing worth a test here.
"""

import importlib.util
import pathlib
import random

import pytest

_MODULE = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "demo" / "odp_dataset.py"


@pytest.fixture(scope="module")
def odp():
    """The generator, loaded by path: deploy/demo is scripts, not a package."""
    spec = importlib.util.spec_from_file_location("odp_dataset", _MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_row_matches_its_column_list(odp):
    rng = random.Random(7)
    seen = set()
    for table, row in odp._project_rows(0, rng):
        assert len(row) == len(odp.COLUMNS[table]), (
            f"{table}: {len(row)} values against {len(odp.COLUMNS[table])} columns"
        )
        seen.add(table)
    for table, row in odp._software_rows(3, rng):
        assert len(row) == len(odp.COLUMNS[table]), (
            f"{table}: {len(row)} values against {len(odp.COLUMNS[table])} columns"
        )
        seen.add(table)
    assert seen == set(odp.ORDER), f"tables never generated: {set(odp.ORDER) - seen}"


def test_one_project_yields_the_advertised_fan_out(odp):
    """PRODUCTS_PER_PROJECT is what the CLI quotes when sizing a run."""
    counts: dict[str, int] = {}
    for table, _ in odp._project_rows(0, random.Random(7)):
        counts[table] = counts.get(table, 0) + 1
    assert counts["projects"] == 1
    assert counts["data_products"] == odp.PRODUCTS_PER_PROJECT
    assert counts["artifacts"] == odp.PRODUCTS_PER_PROJECT * odp.ARTIFACTS_PER_PRODUCT


def test_products_carry_geometry_and_artifacts_do_not(odp):
    """ivoa.obscore takes the footprint from the data product and joins
    artifacts only for access metadata, which is how the model's own example
    payload is shaped. An artifact that grew a footprint here would be
    duplicating the product's, at 32 vertices a row."""
    geom_index = odp.PRODUCT_COLUMNS.index("s_region_geom")
    products = [r for t, r in odp._project_rows(0, random.Random(7)) if t == "data_products"]
    assert all(row[geom_index] is not None for row in products)
    assert all(row[geom_index].startswith("{(") for row in products)

    artifact_geom = odp.ARTIFACT_COLUMNS.index("s_region_geom")
    artifacts = [r for t, r in odp._project_rows(0, random.Random(7)) if t == "artifacts"]
    assert artifacts and all(row[artifact_geom] is None for row in artifacts)
    url = odp.ARTIFACT_COLUMNS.index("access_url")
    assert all(row[url] for row in artifacts), "obscore reads access_url through the join"


def test_generation_is_deterministic(odp):
    """Same seed, same corpus: a reload after a failure has to reproduce the
    rows a snapshot or a half-finished load already has."""
    first = list(odp._project_rows(3, random.Random(99)))
    second = list(odp._project_rows(3, random.Random(99)))
    assert first == second


def test_values_respect_the_schema_check_constraints(odp):
    """The columns the ODP tables constrain — a violation only shows up as a
    failed COPY several minutes into a load."""
    cols = odp.PRODUCT_COLUMNS
    rng = random.Random(11)
    for table, row in odp._project_rows(0, rng):
        if table != "data_products":
            continue
        value = dict(zip(cols, row, strict=True))
        assert 0.0 <= value["s_ra"] <= 360.0
        assert -90.0 <= value["s_dec"] <= 90.0
        assert 0.0 < value["s_fov"] <= 10.0
        assert 0.0195 <= value["em_min"] <= 6.0
        assert 0.0195 <= value["em_max"] <= 6.0
        assert value["em_wlen"] >= 0.0
        assert 0 <= value["calib_level"] <= 3
        assert value["t_min"] >= 0 and value["t_max"] >= value["t_min"]
        assert -180.0 <= value["beam_pa"] <= 180.0
        assert value["dataproduct_type"] in odp.DATAPRODUCT_TYPES
        assert value["data_product_origin"] in ("ODP", "ADP")
        # nullable, but constrained to the enum when present
        assert value["calibrator_type"] in (None, *odp.CALIBRATOR_TYPES)
