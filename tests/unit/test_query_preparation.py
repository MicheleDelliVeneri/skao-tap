"""Query preparation: one ADQL parse, and one TAP_SCHEMA read per window.

The cost being guarded here is real: translating an ADQL query is tens of
milliseconds of pure-Python ANTLR work, and the publication check used to add
a second, larger parse on top of it. These tests pin the behaviour that made
the second one unnecessary, and the caching that stopped the table list being
re-read on every request.
"""

import pytest
from tap_api.queries import query as query_module
from tapcore.query.adql import touched_tables, translate

QUERIES = {
    "point": "SELECT source_id FROM ska.continuum_sources WHERE source_id = 5",
    "cone": (
        "SELECT source_id, ra, dec FROM ska.continuum_sources "
        "WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 62.3, -65.5, 1.0))"
    ),
    "join": (
        "SELECT s.ra, t.table_name FROM ska.continuum_sources AS s "
        "JOIN tap_schema.tables AS t ON t.table_name = 'ska.continuum_sources'"
    ),
    "upload": "SELECT TOP 10 a.source_id FROM TAP_UPLOAD.mine AS a",
    "subquery": ("SELECT x.ra FROM (SELECT ra FROM ska.continuum_sources) AS x WHERE x.ra > 1"),
}


# -- the tables come from the parse we already did ---------------------------


@pytest.mark.parametrize("label", sorted(QUERIES))
def test_the_tree_walk_agrees_with_re_parsing_the_sql(label):
    """The second parse is what this replaces, so it is the reference."""
    translation = translate(QUERIES[label])
    from_sql = touched_tables(translation.sql)
    assert {name.lower() for name in translation.tables} == {name.lower() for name in from_sql}, (
        label
    )


def test_an_upload_reference_survives_the_walk():
    """The publication check special-cases TAP_UPLOAD, so the prefix has to
    reach it intact."""
    assert translate(QUERIES["upload"]).tables == frozenset({"TAP_UPLOAD.mine"})


def test_a_join_reports_every_table():
    assert translate(QUERIES["join"]).tables == frozenset(
        {"ska.continuum_sources", "tap_schema.tables"}
    )


def test_a_syntax_error_still_raises_before_any_table_lookup():
    from tapcore.errors import QueryParseError

    with pytest.raises(QueryParseError, match="syntax error"):
        translate("SELEC nonsense FROM nowhere")


def test_the_table_walk_failing_does_not_fail_the_query(monkeypatch):
    """A grammar change must degrade to "unknown tables", where the database
    permission layer is the backstop — not reject a query the translator
    accepted."""

    from tapcore.query import adql

    def boom():
        raise RuntimeError("grammar moved")

    monkeypatch.setattr(adql, "_TableCollector", boom)
    translation = translate(QUERIES["point"])
    assert translation.tables == frozenset()
    assert "continuum_sources" in translation.sql


# -- the published-table list is read once per window -----------------------


@pytest.fixture
def counting_pool(monkeypatch, fake_db):
    """Count how many times the published-table query reaches the database."""
    query_module.forget_published_tables()
    calls = {"n": 0}
    original = query_module.pool

    class CountingConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if "tap_schema.tables" in sql:
                calls["n"] += 1
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class CountingPool:
        def connection(self):
            import contextlib

            @contextlib.contextmanager
            def wrapped():
                with original().connection() as conn:
                    yield CountingConnection(conn)

            return wrapped()

    monkeypatch.setattr(query_module, "pool", CountingPool)
    yield calls
    query_module.forget_published_tables()


def test_the_table_list_is_read_once_not_per_call(counting_pool):
    for _ in range(5):
        query_module._published_tables()
    assert counting_pool["n"] == 1


def test_expiry_re_reads_the_table_list(counting_pool, monkeypatch):
    query_module._published_tables()
    now = [0.0]
    monkeypatch.setattr(query_module.time, "monotonic", lambda: now[0])
    query_module.forget_published_tables()
    query_module._published_tables()
    now[0] += query_module._PUBLISHED_TTL_S + 1
    query_module._published_tables()
    assert counting_pool["n"] == 3


def test_invalidation_forces_a_re_read(counting_pool):
    query_module._published_tables()
    query_module.forget_published_tables()
    query_module._published_tables()
    assert counting_pool["n"] == 2


def test_a_table_published_out_of_band_appears_when_the_window_expires(counting_pool, monkeypatch):
    """The window is the backstop for a publication this service did not make;
    the refresh-on-miss path above is what makes it not matter in practice."""
    now = [1000.0]
    monkeypatch.setattr(query_module.time, "monotonic", lambda: now[0])
    query_module.forget_published_tables()
    assert "ska.late_table" not in query_module._published_tables()

    query_module._published_tables()  # still cached, still stale

    now[0] += query_module._PUBLISHED_TTL_S + 1
    before = counting_pool["n"]
    query_module._published_tables()
    assert counting_pool["n"] == before + 1, "expiry must force a re-read"


def test_a_table_published_while_running_is_not_refused(counting_pool, client, fake_db):
    """What the component tests caught: a fixture registers a table in
    TAP_SCHEMA while the service is up, and a cached "not published" would
    reject queries against it until the window expired."""
    query_module._published_tables()  # warm the cache without the new table
    fake_db.published.append(("ska.late_arrival",))
    reads_before = counting_pool["n"]

    published = query_module._published_tables()
    assert "ska.late_arrival" not in published  # stale, as expected

    # asking about it refreshes rather than refuses
    assert (
        query_module._first_unpublished(frozenset({"ska.late_arrival"}), published, set())
        == "ska.late_arrival"
    )
    query_module.forget_published_tables()
    assert "ska.late_arrival" in query_module._published_tables()
    assert counting_pool["n"] > reads_before
