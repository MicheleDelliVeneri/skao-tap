"""ObsCore 1.1 end to end (package 12): the odp bootstrap derives the
ivoa.obscore view, TAP publishes and serves it, and the declarations a
validator reads (capabilities, /tables) say so."""

import copy

import httpx
import pytest
from ska_src_mm_notification.models.schemas.srcnet_ingestion import (
    SRC_INGESTION_EXAMPLE,
)

pytestmark = pytest.mark.component


def _sync_csv(tap_service: str, adql: str) -> list[str]:
    response = httpx.post(
        f"{tap_service}/sync",
        data={"LANG": "ADQL", "QUERY": adql, "RESPONSEFORMAT": "csv"},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    return response.text.strip().splitlines()


def test_obscore_view_serves_ingested_products(tap_service, api_url):
    payload = copy.deepcopy(SRC_INGESTION_EXAMPLE)
    payload["project_id"] = "obscore-demo"
    product = payload["observations"][0]["scheduling_blocks"][0]["execution_blocks"][0][
        "data_products"
    ][0]
    product["s_region"] = "CIRCLE ICRS 150.0 -30.0 0.5"
    created = httpx.post(f"{api_url}/notifications", json=payload, timeout=30)
    assert created.status_code == 201, created.text

    obs_id = payload["observations"][0]["obs_id"]

    # the REC's name is case-insensitive: ivoa.ObsCore works unquoted
    lines = _sync_csv(
        tap_service,
        "SELECT obs_publisher_did, obs_collection, dataproduct_type, calib_level"
        f" FROM ivoa.ObsCore WHERE obs_id = '{obs_id}'",
    )
    header, rows = lines[0].split(","), lines[1:]
    assert header == ["obs_publisher_did", "obs_collection", "dataproduct_type", "calib_level"]
    assert any(row.startswith(f"ivo://skao.int/~?obscore-demo/{obs_id}/") for row in rows)

    # the geometry companion answers footprint queries on the view itself
    overlap = _sync_csv(
        tap_service,
        "SELECT obs_publisher_did FROM ivoa.obscore"
        " WHERE 1=INTERSECTS(s_region_geom, CIRCLE('ICRS', 150.2, -30.0, 0.1))",
    )
    assert any("obscore-demo" in row for row in overlap[1:])


def test_obscore_declarations_for_validators(tap_service, api_url):
    capabilities = httpx.get(f"{tap_service}/capabilities", timeout=10).text
    assert (
        '<dataModel ivo-id="ivo://ivoa.net/std/ObsCore#core-1.1">ObsCore-1.1</dataModel>'
        in capabilities
    )

    tables = httpx.get(f"{tap_service}/tables", timeout=10).text
    assert "<name>ivoa.obscore</name>" in tables
    assert "<utype>ivo://ivoa.net/std/ObsCore#core-1.1</utype>" in tables
    assert 'extendedType="adql:REGION"' in tables
    assert "<utype>obscore:Curation.publisherDID</utype>" in tables

    listing = httpx.get(f"{api_url}/tables", timeout=10).json()
    obscore = next(t for t in listing["tables"] if t["name"] == "ivoa.obscore")
    assert obscore["utype"] == "ivo://ivoa.net/std/ObsCore#core-1.1"
    names = [c["name"] for c in obscore["columns"]]
    assert names[:5] == [
        "dataproduct_type",
        "calib_level",
        "obs_collection",
        "obs_id",
        "obs_publisher_did",
    ]
    assert "s_region_geom" in names

    examples = httpx.get(f"{tap_service}/examples", timeout=10).text
    assert "ivoa.obscore" in examples


def test_obscore_publisher_did_percent_encodes_the_key_chain(tap_service, api_url):
    """The key columns are free text and a PublisherDID is permanent: a
    product_id carrying a space, a '/' and a '#' must not forge a sixth path
    segment or truncate the identifier at the fragment."""
    payload = copy.deepcopy(SRC_INGESTION_EXAMPLE)
    payload["project_id"] = "obscore-escape"
    product = payload["observations"][0]["scheduling_blocks"][0]["execution_blocks"][0][
        "data_products"
    ][0]
    product["product_id"] = "cube 3/a#1"
    created = httpx.post(f"{api_url}/notifications", json=payload, timeout=30)
    assert created.status_code == 201, created.text

    lines = _sync_csv(
        tap_service,
        "SELECT obs_publisher_did FROM ivoa.obscore"
        " WHERE obs_publisher_did LIKE '%obscore-escape%'",
    )
    dids = lines[1:]
    assert dids, lines
    for did in dids:
        assert did.startswith("ivo://skao.int/~?obscore-escape/")
        assert did.endswith("/cube%203%2Fa%231")
        # the raw characters never reach the identifier
        assert " " not in did and "#" not in did


def test_a_geometry_predicate_over_the_text_column_is_a_usage_error(tap_service):
    """`s_region` is the column every ObsCore tutorial names, and it holds
    STC-S text: the queryable companion is the non-standard `s_region_geom`.

    Translation is pure and consults no schema, so the standard spelling used
    to translate happily and die inside PostgreSQL as
    `operator does not exist: text && scircle` — a 500-shaped answer to what
    is a usage error, naming neither the column at fault nor the one to use.
    """
    response = httpx.post(
        f"{tap_service}/sync",
        data={
            "LANG": "ADQL",
            "QUERY": (
                "SELECT obs_publisher_did FROM ivoa.obscore "
                "WHERE 1 = INTERSECTS(s_region, CIRCLE('ICRS', 150.0, -30.0, 0.5))"
            ),
            "RESPONSEFORMAT": "csv",
        },
        timeout=30,
    )
    assert response.status_code == 400, response.text
    body = response.text
    assert "s_region" in body
    assert "s_region_geom" in body, "the usable column has to be named"
    assert "NULL" in body, "the companion is nullable; a caller must know that"
    # the failure a client used to get, and must not get any more
    assert "operator does not exist" not in body


def test_the_geometry_companion_is_still_accepted(tap_service):
    """The check must refuse only what PostgreSQL would refuse."""
    response = httpx.post(
        f"{tap_service}/sync",
        data={
            "LANG": "ADQL",
            "QUERY": (
                "SELECT obs_publisher_did FROM ivoa.obscore "
                "WHERE 1 = INTERSECTS(s_region_geom, CIRCLE('ICRS', 150.0, -30.0, 0.5))"
            ),
            "RESPONSEFORMAT": "csv",
        },
        timeout=30,
    )
    assert response.status_code == 200, response.text


def _plan(database_url: str, sql: str, *settings: str) -> str:
    import psycopg

    with psycopg.connect(database_url) as conn:
        for setting in settings:
            conn.execute(setting)
        rows = conn.execute(f"EXPLAIN (COSTS OFF) {sql}").fetchall()
    return "\n".join(row[0] for row in rows)


_ONLY_BITMAP_SCANS = (
    "SET enable_seqscan = off",
    "SET enable_indexscan = off",
    "SET enable_indexonlyscan = off",
)


def test_a_did_lookup_can_use_the_trigram_index(tap_service, api_url, database_url):
    """A leading-wildcard LIKE on obs_publisher_did used to evaluate the
    five-way DID expression, correlated subqueries included, for every
    product: no index could hold that expression. Now the expression is a
    function call and the bootstrap indexes it with pg_trgm — and the index
    is only ever used if its expression is structurally the view's, which is
    what the plan below proves. Sequential scans, and the filtered scans of
    another index that stand in for one, are switched off because a planner
    facing a handful of rows would rightly prefer either; what is left is the
    bitmap scan a GIN index provides — if its expression matches."""
    payload = copy.deepcopy(SRC_INGESTION_EXAMPLE)
    payload["project_id"] = "obscore-lookup"
    created = httpx.post(f"{api_url}/notifications", json=payload, timeout=30)
    assert created.status_code == 201, created.text

    for pattern in ("%obscore-lookup/%", "ivo://skao.int/~?obscore-lookup/%"):
        plan = _plan(
            database_url,
            f"SELECT obs_id FROM ivoa.obscore WHERE obs_publisher_did LIKE '{pattern}'",
            *_ONLY_BITMAP_SCANS,
        )
        assert "data_products_obscore_did_trgm" in plan, plan

    # and the rows an index scan yields are the rows: the whole key chain,
    # reachable by equality as well
    observation = payload["observations"][0]
    block = observation["scheduling_blocks"][0]["execution_blocks"][0]
    did = "ivo://skao.int/~?obscore-lookup/{}/{}/{}/{}".format(
        observation["obs_id"],
        observation["scheduling_blocks"][0]["sbd_id"],
        block["eb_id"],
        block["data_products"][0]["product_id"],
    )
    lines = _sync_csv(
        tap_service, f"SELECT obs_publisher_did FROM ivoa.obscore WHERE obs_publisher_did = '{did}'"
    )
    assert lines[1:] == [did], lines
    plan = _plan(
        database_url,
        f"SELECT obs_id FROM ivoa.obscore WHERE obs_publisher_did = '{did}'",
        *_ONLY_BITMAP_SCANS,
    )
    assert "data_products_obscore_did_trgm" in plan, plan


def test_a_query_that_reads_no_access_column_never_touches_artifacts(tap_service, database_url):
    """The access columns come from a LEFT JOIN to one artifact per product.
    A GROUP BY over the view that reads none of them used to probe
    srcnet.artifacts once per matching product all the same, because a
    `LIMIT 1` subquery is not one the planner can prove single-row and so
    not one it can remove. The aggregate form is."""
    plan = _plan(
        database_url,
        "SELECT obs_collection, dataproduct_type, count(*), min(t_min), max(t_max)"
        " FROM ivoa.obscore WHERE calib_level >= 2 GROUP BY obs_collection, dataproduct_type",
    )
    assert "artifacts" not in plan, plan
    # a query that does read them still gets them, from the artifact it always did
    plan = _plan(database_url, "SELECT access_url FROM ivoa.obscore")
    assert "artifacts" in plan, plan


def test_the_view_join_has_a_foreign_key_to_estimate_from(tap_service, database_url):
    """data_products joins observations two levels up the hierarchy. The
    planner estimates a join from the foreign keys between the two relations
    joined, and with none it multiplied the selectivities of key columns that
    are perfectly correlated — 10,000x low on the comparison corpus, enough
    to sort-and-group where a hash aggregate belonged."""
    import psycopg

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'srcnet.data_products'::regclass"
            " AND conname = 'data_products_observations_fkey'"
        ).fetchone()
    assert row is not None
    assert row[0].startswith(
        "FOREIGN KEY (project_id, obs_id) REFERENCES srcnet.observations(project_id, obs_id)"
    )
