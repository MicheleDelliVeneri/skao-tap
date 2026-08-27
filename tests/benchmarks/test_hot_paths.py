"""CPU-focused benchmarks for the TAP query hot paths.

These benchmarks deliberately avoid network and database I/O so CI runs can
be compared without mixing in service or storage latency. End-to-end database performance is
covered by the separate PostgreSQL performance workflow.
"""

import asyncio
import datetime
import itertools
import types
from decimal import Decimal
from urllib.parse import urlencode

import pytest

# pytest collects everything under tests/ (see pyproject.toml testpaths), and
# the benchmark fixture comes from the plugin — without it every test here is
# an error on a plain `pytest` run. Skip the module instead.
pytest.importorskip("pytest_benchmark", reason="benchmarks need pytest-benchmark")

from egernia_core import db as db_mod
from egernia_core.query.adql import adql_to_postgresql, touched_tables, translate
from egernia_core.query.results import ColumnMeta, RowLimiter, stream
from queryparser.adql import ADQLQueryTranslator

CONE_SEARCH = (
    "SELECT source_id, ra, dec, flux FROM ska.continuum_sources "
    "WHERE 1=CONTAINS(POINT('ICRS', ra, dec), "
    "CIRCLE('ICRS', 62.3, -65.5, 1.0))"
)
JOIN_QUERY = (
    "SELECT s.source_id, s.ra, s.dec, t.table_name "
    "FROM ska.continuum_sources AS s "
    "JOIN tap_schema.tables AS t ON t.table_name = 'ska.continuum_sources'"
)
COLUMNS = [
    ColumnMeta("source_id", kind="int64", ucd="meta.id"),
    ColumnMeta("name", kind="str"),
    ColumnMeta("flux", kind="decimal", unit="mJy"),
    ColumnMeta("observed_at", kind="timestamp"),
    ColumnMeta("validated", kind="bool"),
]
ROWS = tuple(
    (
        index,
        f"source-{index}",
        Decimal(f"{index % 100}.{index % 10}"),
        datetime.datetime(2026, 1, 1) + datetime.timedelta(seconds=index),
        index % 2 == 0,
    )
    for index in range(1_000)
)


def _translate_and_inspect() -> set[str]:
    """The old shape, spelled out rather than routed through translate().

    adql_to_postgresql() now walks the parse tree as well, so calling it here
    would measure today's work plus the re-parse and stop being comparable to
    the historical baseline this exists to sit beside.
    """
    translator = ADQLQueryTranslator(JOIN_QUERY)
    return touched_tables(translator.to_postgresql())


def _translate_single_pass() -> set[str]:
    """What the service does now: both from one parse."""
    return set(translate(JOIN_QUERY).tables)


def _serialize(fmt: str) -> bytes:
    limiter = RowLimiter(iter(ROWS), len(ROWS))
    return b"".join(stream(COLUMNS, limiter, fmt))


def test_benchmark_adql_geometry_translation(benchmark):
    sql = benchmark(adql_to_postgresql, CONE_SEARCH).lower()
    assert "spoint" in sql
    assert "scircle" in sql


def test_benchmark_adql_translation_and_table_inspection(benchmark):
    """Kept as the reference the single-pass benchmark below is measured
    against — this is what a request used to cost."""
    tables = benchmark(_translate_and_inspect)
    assert tables == {"ska.continuum_sources", "tap_schema.tables"}


def test_benchmark_adql_translation_single_pass(benchmark):
    tables = benchmark(_translate_single_pass)
    assert tables == {"ska.continuum_sources", "tap_schema.tables"}


def test_benchmark_votable_serialization(benchmark):
    body = benchmark(_serialize, "votable")
    assert body.startswith(b"<?xml")
    assert body.count(b"<TR>") == len(ROWS)


def test_benchmark_json_serialization(benchmark):
    body = benchmark(_serialize, "json")
    assert b'"status":"OK"' in body.replace(b" ", b"")


# ---------------------------------------------------------------------------
# The whole request, end to end (package 18)
# ---------------------------------------------------------------------------
#
# The benchmarks above measure three functions. A regression anywhere else on
# the request path — parameter parsing, the published-table check, the format
# negotiation, the streaming response, the observability instrumentation — was
# invisible to them, which is how a service whose ceiling had been attributed
# to ADQL translation kept that attribution for months after translation
# stopped being 41 ms of it.
#
# So this drives the real ASGI application the way uvicorn does: a scope, a
# receive that hands over the form body, a send that collects the response.
# What is deliberately *not* here:
#
# * PostgreSQL, and libpq with it. The connection is stubbed and its rows are
#   built once at import, so psycopg's per-request row conversion is excluded
#   rather than imitated — a Python re-implementation of a C conversion would
#   be a fabricated cost, and a wrong one.
# * uvicorn, h11 and the socket. The application is called directly, so the
#   HTTP parse and the write are absent.
#
# Both are named subsystems in the cluster profile
# (the since-removed cluster harness's py-spy collector), so the residual
# between this figure and the
# measured per-request CPU is attributable rather than mysterious. That
# comparison is what the number is for: it is a hot-path guard whose scale can
# be checked against a saturation measurement, not an SLO.

# Row shapes and counts as the D1 saturation windows of run
# 20260825T005436Z-b450b0a9 actually measured them: per class, the mean
# successful response over ~150k requests, divided by that class's row width.
# Not the TOP clause — a cone search with TOP 500 returned around 233 rows,
# and sizing the writers by the limit rather than by the result would make
# every cone class several times its measured weight.
OBSCORE_FIELDS = (
    ("obs_publisher_did", 25),
    ("obs_id", 25),
    ("obs_collection", 25),
    ("dataproduct_type", 25),
    ("calib_level", 23),
    ("s_ra", 701),
    ("s_dec", 701),
    ("s_fov", 701),
    ("t_min", 701),
    ("t_max", 701),
    ("access_url", 25),
)
ODP_JOIN_FIELDS = (
    ("obs_id", 25),
    ("collection", 25),
    ("instrument_name", 25),
    ("product_id", 25),
    ("calib_level", 23),
    ("dataproduct_type", 25),
)
METADATA_FIELDS = (("table_name", 25), ("description", 25))


def _obscore_row(index: int) -> tuple:
    return (
        f"ivo://skao.int/~?SKA-LOW/obs:{index:012d}",
        f"ska:obs:{index:012d}",
        "SKA-LOW",
        "cube",
        2,
        123.45678 + index * 1e-4,
        -45.6789 + index * 1e-4,
        0.75,
        59000.0 + index * 1e-3,
        59000.5 + index * 1e-3,
        f"https://data.skao.int/products/{index:012d}/cube.fits",
    )


def _odp_row(index: int) -> tuple:
    return (
        f"SKA-Mid-{index:09d}-00",
        "SKAO/SKA-Mid",
        "SKA-Mid",
        f"eb-{index:09d}-00-00-000-000",
        2,
        "visibility",
    )


def _metadata_row(index: int) -> tuple:
    return (
        f"ska.table_{index}",
        "A published table generated from an active metadata plugin.",
    )


# (query class, ADQL, cursor fields, row count) — the corpus classes of the
# normal mix, with the row counts above.
CLASSES = {
    "Q01": (
        "SELECT table_name, description FROM tap_schema.tables",
        METADATA_FIELDS,
        _metadata_row,
        12,
    ),
    "Q02": (
        "SELECT obs_publisher_did, obs_id, obs_collection, dataproduct_type, calib_level,"
        " s_ra, s_dec, s_fov, t_min, t_max, access_url FROM ivoa.obscore"
        " WHERE obs_id = 'ska:obs:000000123456'",
        OBSCORE_FIELDS,
        _obscore_row,
        2,
    ),
    "Q03": (
        "SELECT TOP 100 obs_publisher_did, obs_id, obs_collection, dataproduct_type,"
        " calib_level, s_ra, s_dec, s_fov, t_min, t_max, access_url FROM ivoa.obscore"
        " WHERE obs_collection = 'SKA-LOW' AND dataproduct_type = 'cube'",
        OBSCORE_FIELDS,
        _obscore_row,
        100,
    ),
    "Q04": (
        "SELECT TOP 200 obs_publisher_did, obs_id, obs_collection, dataproduct_type,"
        " calib_level, s_ra, s_dec, s_fov, t_min, t_max, access_url FROM ivoa.obscore"
        " WHERE t_min > 59000.0 AND t_max < 59073.0",
        OBSCORE_FIELDS,
        _obscore_row,
        200,
    ),
    "Q05": (
        "SELECT TOP 100 obs_publisher_did, obs_id, obs_collection, dataproduct_type,"
        " calib_level, s_ra, s_dec, s_fov, t_min, t_max, access_url FROM ivoa.obscore"
        " WHERE 1 = CONTAINS(POINT('ICRS', s_ra, s_dec),"
        " CIRCLE('ICRS', 123.45678, -45.6789, 0.15))",
        OBSCORE_FIELDS,
        _obscore_row,
        3,
    ),
    "Q06": (
        "SELECT TOP 500 obs_publisher_did, obs_id, obs_collection, dataproduct_type,"
        " calib_level, s_ra, s_dec, s_fov, t_min, t_max, access_url FROM ivoa.obscore"
        " WHERE 1 = CONTAINS(POINT('ICRS', s_ra, s_dec),"
        " CIRCLE('ICRS', 123.45678, -45.6789, 2.0))",
        OBSCORE_FIELDS,
        _obscore_row,
        233,
    ),
    "Q07": (
        "SELECT TOP 200 obs_publisher_did, obs_id, obs_collection, dataproduct_type,"
        " calib_level, s_ra, s_dec, s_fov, t_min, t_max, access_url FROM ivoa.obscore"
        " WHERE 1 = CONTAINS(POINT('ICRS', s_ra, s_dec),"
        " CIRCLE('ICRS', 123.45678, -45.6789, 1.0))"
        " AND t_min > 59000.0 AND calib_level >= 1",
        OBSCORE_FIELDS,
        _obscore_row,
        39,
    ),
    "Q08": (
        "SELECT TOP 200 o.obs_id, o.collection, o.instrument_name, p.plane_id,"
        " p.calib_level, p.dataproduct_type FROM srcnet.observations AS o"
        " JOIN srcnet.data_products AS p"
        " ON o.project_id = p.project_id AND o.obs_id = p.obs_id"
        " WHERE o.collection = 'SKA-MID' AND p.calib_level = 2",
        ODP_JOIN_FIELDS,
        _odp_row,
        200,
    ),
    "Q10": (
        "SELECT TOP 1000 obs_publisher_did, obs_id, obs_collection, dataproduct_type,"
        " calib_level, s_ra, s_dec, s_fov, t_min, t_max, access_url FROM ivoa.obscore"
        " WHERE obs_collection = 'SKA-LOW' AND t_min > 59000.0",
        OBSCORE_FIELDS,
        _obscore_row,
        1000,
    ),
}

# config/scenarios.yaml's `query_mix.normal`, as the smallest whole number of
# requests that expresses it exactly. One benchmark iteration is one request
# drawn from this cycle in order, so the mean over a run is the mix-weighted
# mean per-request cost — which is the figure a saturation throughput is the
# reciprocal of.
MIX = {
    "Q01": 1,
    "Q02": 3,
    "Q03": 3,
    "Q04": 2,
    "Q05": 5,
    "Q06": 2,
    "Q07": 2,
    "Q08": 1,
    "Q10": 1,
}
MIX_CYCLE = tuple(cls for cls, count in MIX.items() for _ in range(count))

# Built once, at import. psycopg produces these per request in C; rebuilding
# them per request in Python would add a cost the service does not pay and
# call it row conversion.
_ROWS = {
    cls: tuple(builder(index) for index in range(rows))
    for cls, (_sql, _fields, builder, rows) in CLASSES.items()
}
_DESCRIPTIONS = {
    cls: tuple(types.SimpleNamespace(name=name, type_code=oid) for name, oid in CLASSES[cls][1])
    for cls in CLASSES
}

# What tap_schema.columns would return for the touched tables, and what
# tap_schema.tables would list. Constant per request in the service too — the
# table list is cached for 30 seconds and the column metadata is one indexed
# read — so the stub answers them without pretending to be a database.
_TAP_SCHEMA_COLUMNS = tuple(
    (name, "deg" if name.startswith("s_") else None, f"pos.eq.{name}", f"the {name} column")
    for name, _oid in OBSCORE_FIELDS + ODP_JOIN_FIELDS + METADATA_FIELDS
)
_PUBLISHED_TABLES = (
    ("ivoa.obscore",),
    ("srcnet.projects",),
    ("srcnet.observations",),
    ("srcnet.scheduling_blocks",),
    ("srcnet.execution_blocks",),
    ("srcnet.data_products",),
    ("srcnet.artifacts",),
    ("tap_schema.tables",),
    ("tap_schema.columns",),
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Cursor:
    """Enough of a psycopg cursor for the streaming result path.

    `description` is set by `stream()` rather than at construction because that
    is the order the real path relies on: `StreamedRows` sends the query and
    waits for the first chunk precisely so the description is populated before
    any row is consumed.
    """

    def __init__(self, state):
        self._state = state
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        return _Result([])

    def stream(self, sql, size=1):
        self.description = _DESCRIPTIONS[self._state["class"]]
        yield from _ROWS[self._state["class"]]


class _Connection:
    def __init__(self, state):
        self._state = state

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def transaction(self):
        return self

    def cursor(self):
        return _Cursor(self._state)

    def execute(self, sql, params=None):
        text = sql if isinstance(sql, str) else str(sql)
        if "tap_schema.columns" in text:
            return _Result(list(_TAP_SCHEMA_COLUMNS))
        if "tap_schema.tables" in text:
            return _Result(list(_PUBLISHED_TABLES))
        return _Result([])  # set_config, SET LOCAL jit/ROLE


class _Pool:
    def __init__(self, state):
        self._state = state

    def connection(self, timeout=None):
        return _Connection(self._state)


def _scope(body: bytes) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/tap/sync",
        "raw_path": b"/tap/sync",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"benchmark"),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 51000),
        "server": ("benchmark", 8080),
    }


@pytest.fixture
def sync_request(monkeypatch):
    """One callable that runs one /tap/sync request through the real app."""
    from egernia_api.main import app
    from egernia_api.queries import query as query_mod

    state = {"class": "Q05"}
    monkeypatch.setattr(db_mod, "pool", lambda: _Pool(state))
    # The 30-second cache would otherwise carry a table list built by whichever
    # test ran first, and `forget_published_tables` is what the service calls
    # when the list can no longer be trusted.
    query_mod.forget_published_tables()

    bodies = {
        cls: urlencode({"LANG": "ADQL", "QUERY": sql, "RESPONSEFORMAT": "csv"}).encode()
        for cls, (sql, _f, _b, _n) in CLASSES.items()
    }
    loop = asyncio.new_event_loop()

    async def one(cls: str) -> tuple[int, int]:
        state["class"] = cls
        body = bodies[cls]
        sent: list[dict] = []
        delivered = False

        async def receive():
            """The body once, then nothing — never a disconnect.

            A second call has to block rather than answer. StreamingResponse
            races `listen_for_disconnect` against the body generator and stops
            at the first disconnect it sees, so a receive that reports one as
            soon as the body is consumed produces a 200 with an empty body and
            a benchmark that measures the response headers.
            """
            nonlocal delivered
            if delivered:
                await asyncio.Event().wait()  # cancelled with the task group
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        await app(_scope(body), receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        size = sum(len(m.get("body", b"")) for m in sent if m["type"] == "http.response.body")
        return status, size

    def run(cls: str) -> tuple[int, int]:
        return loop.run_until_complete(one(cls))

    try:
        yield run
    finally:
        loop.close()


@pytest.mark.parametrize("query_class", sorted(CLASSES))
def test_benchmark_sync_request_by_class(benchmark, sync_request, query_class):
    """One /tap/sync request of one class, application-side end to end."""
    status, size = benchmark(sync_request, query_class)
    assert status == 200
    assert size > 0


def test_benchmark_sync_request_normal_mix(benchmark, sync_request):
    """The normal mix, one request per iteration, drawn in the mix's proportions.

    This is the benchmark whose mean is comparable to a measured saturation
    throughput: at `replicas: 1, workers: 1` the service serves one request at
    a time, so its ceiling is the reciprocal of the mix-weighted per-request
    cost. The two are not the same measurement and are not expected to agree
    exactly — this one has no database and no HTTP server in it — so the
    comparison belongs in a finding beside the profile that says how large
    those two omissions are.
    """
    cycle = itertools.cycle(MIX_CYCLE)

    def one_request():
        return sync_request(next(cycle))

    status, size = benchmark(one_request)
    assert status == 200
    assert size > 0
