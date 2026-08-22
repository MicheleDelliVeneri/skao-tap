"""Unit-test fixtures: an in-memory stand-in for the psycopg pool.

The fake routes the small set of SQL shapes the services issue (the uws.jobs
CRUD, TAP_SCHEMA lookups, srcnet upserts) to an in-memory store, so the HTTP
layer, the UWS job lifecycle, and the executor can be exercised without a
PostgreSQL server.
"""

import contextlib
import datetime
import json
import re

import pytest
import tapcore.db
from tapcore import uws

JOB_KEYS = [c.strip() for c in uws.JOB_COLUMNS.split(",")]


class FakeColumn:
    """Cursor description entry (name + type OID)."""

    def __init__(self, name, type_code):
        self.name = name
        self.type_code = type_code


class FakeResult:
    def __init__(self, rows=None, rowcount=None):
        self._rows = rows or []
        self.rowcount = len(self._rows) if rowcount is None else rowcount

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeServerCursor:
    """Named (server-side) cursor streaming the configured result set."""

    def __init__(self, db):
        self._db = db
        self.itersize = None
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._db.statements.append(sql.strip())
        if self._db.result_error is not None:
            raise self._db.result_error
        self.description = self._db.result_description
        self._rows = list(self._db.result_rows)

    def __iter__(self):
        return iter(self._rows)


class FakeConnection:
    def __init__(self, db):
        self._db = db

    @contextlib.contextmanager
    def transaction(self):
        yield self

    def cursor(self, name=None):
        return FakeServerCursor(self._db)

    def commit(self):
        pass

    def execute(self, sql, params=None):
        return self._db.execute(sql, params)


class FakePool:
    def __init__(self, db):
        self._db = db

    @contextlib.contextmanager
    def connection(self):
        yield FakeConnection(self._db)

    def close(self):
        self._db.closed = True


def _job_row(job: dict) -> tuple:
    return tuple(job[k] for k in JOB_KEYS)


class FakeDB:
    """Routes executed SQL to an in-memory job store and canned metadata."""

    def __init__(self):
        now = datetime.datetime.now(datetime.UTC)
        self.closed = False
        self.cancelled: list[int] = []
        self.jobs: dict[str, dict] = {}
        self.srcnet: dict[str, dict[tuple, dict]] = {}
        self.statements: list[str] = []
        # published tables (query.py permission check)
        self.published = [("ska.continuum_sources",), ("tap_schema.tables",)]
        # tap_schema.columns annotations (tap_schema_metadata)
        self.column_annotations = [
            ("source_id", None, "meta.id", "source identifier"),
            ("ra", "deg", "pos.eq.ra", "right ascension"),
        ]
        # result set produced by named-cursor queries
        self.result_description = [FakeColumn("source_id", 20), FakeColumn("ra", 701)]
        self.result_rows = [(1, 62.1), (2, 62.2)]
        self.result_error: Exception | None = None
        # VOSI /tables metadata
        self.schemas = [("ska", "SKA continuum catalogues")]
        self.schema_tables = [("ska", "ska.continuum_sources", "table", "continuum sources")]
        self.schema_columns = [
            (
                "ska.continuum_sources",
                "source_id",
                "long",
                None,
                "source identifier",
                None,
                "meta.id",
            ),
            ("ska.continuum_sources", "ra", "double", None, "right ascension", "deg", "pos.eq.ra"),
        ]
        # /api/v1/tables aggregate
        self.json_tables = [
            (
                "ska",
                "ska.continuum_sources",
                "continuum sources",
                [{"name": "source_id", "datatype": "long"}],
            ),
        ]
        self._now = now

    # -- helpers -----------------------------------------------------------

    def add_job(self, **overrides) -> dict:
        job = dict.fromkeys(JOB_KEYS)
        job.update(
            job_id=uws.new_job_id(),
            phase="PENDING",
            creation_time=self._now,
            execution_duration=600,
            destruction=self._now + datetime.timedelta(days=7),
            parameters={},
        )
        job.update(overrides)
        self.jobs[job["job_id"]] = job
        return job

    # -- SQL routing --------------------------------------------------------

    def execute(self, sql, params=None):
        text = sql.strip()
        self.statements.append(text)
        head = text.upper()

        if head.startswith(
            (
                "SELECT SET_CONFIG",
                "SET LOCAL ROLE",
                "SELECT PG_ADVISORY",
                "SELECT 1",
                "ALTER TABLE uws.jobs".upper(),
            )
        ):
            return FakeResult()

        if head.startswith("SELECT PG_BACKEND_PID"):
            return FakeResult([(4242,)])

        if head.startswith("SELECT PG_CANCEL_BACKEND"):
            self.cancelled.append(params[0])
            return FakeResult([(True,)])

        if "FROM pg_stat_activity" in text:
            return FakeResult()  # cancelled backend no longer active

        if text.startswith("SELECT table_name FROM tap_schema.tables"):
            return FakeResult(self.published)
        if text.startswith("SELECT column_name, unit, ucd, description FROM tap_schema.columns"):
            return FakeResult(self.column_annotations)
        if text.startswith("SELECT schema_name, description FROM tap_schema.schemas"):
            return FakeResult(self.schemas)
        if text.startswith("SELECT schema_name, table_name, table_type, description"):
            return FakeResult(self.schema_tables)
        if text.startswith("SELECT table_name, column_name, datatype, arraysize"):
            return FakeResult(self.schema_columns)
        if "jsonb_agg" in text:
            return FakeResult(self.json_tables)

        if head.startswith("INSERT INTO UWS.JOBS"):
            job_id, run_id, owner_id, created, duration, destruction, parameters = params
            self.jobs[job_id] = dict.fromkeys(JOB_KEYS) | {
                "job_id": job_id,
                "phase": "PENDING",
                "run_id": run_id,
                "owner_id": owner_id,
                "creation_time": created,
                "execution_duration": duration,
                "destruction": destruction,
                "parameters": json.loads(parameters),
            }
            return FakeResult(rowcount=1)

        if "FOR UPDATE SKIP LOCKED" in text:  # executor claim
            queued = sorted(
                (j for j in self.jobs.values() if j["phase"] == "QUEUED"),
                key=lambda j: j["creation_time"],
            )
            if not queued:
                return FakeResult()
            job = queued[0]
            job["phase"] = "EXECUTING"
            job["start_time"] = datetime.datetime.now(datetime.UTC)
            return FakeResult([_job_row(job)])

        if head.startswith("SELECT") and "FROM uws.jobs WHERE job_id" in text:
            job = self.jobs.get(params[0])
            return FakeResult([_job_row(job)] if job else [])

        if head.startswith("SELECT") and "FROM uws.jobs" in text:  # list
            index = 0
            if "phase = ANY" in text:
                jobs = [j for j in self.jobs.values() if j["phase"] in params[index]]
                index += 1
            else:
                jobs = [j for j in self.jobs.values() if j["phase"] != "ARCHIVED"]
            if "creation_time >" in text:
                jobs = [j for j in jobs if j["creation_time"] > params[index]]
                index += 1
            jobs.sort(key=lambda j: j["creation_time"], reverse=True)
            if "LIMIT" in text:
                jobs = jobs[: params[index]]
            return FakeResult([_job_row(j) for j in jobs])

        if head.startswith("UPDATE UWS.JOBS SET"):
            names = re.findall(r"(\w+) = %s", text.split("WHERE")[0])
            job = self.jobs.get(params[-1])
            if job is None:
                return FakeResult(rowcount=0)
            for name, value in zip(names, params[:-1], strict=True):
                job[name] = json.loads(value) if name == "parameters" else value
            return FakeResult(rowcount=1)

        if head.startswith("DELETE FROM UWS.JOBS WHERE DESTRUCTION"):
            now = datetime.datetime.now(datetime.UTC)
            expired = [j for j in self.jobs.values() if j["destruction"] < now]
            for job in expired:
                del self.jobs[job["job_id"]]
            return FakeResult([(j["job_id"],) for j in expired])

        if head.startswith("DELETE FROM UWS.JOBS"):
            return FakeResult(rowcount=1 if self.jobs.pop(params[0], None) else 0)

        match = re.match(r"UPDATE (srcnet\.\S+) SET ", text)
        if match:  # srcnet amend
            table = match.group(1)
            set_part, where_part = text.split(" WHERE ", 1)
            set_names = re.findall(r"(\w+) = %s", set_part)
            where_names = re.findall(r"(\w+) = %s", where_part)
            set_values = [
                getattr(p, "obj", None) if type(p).__name__ == "Jsonb" else p
                for p in params[: len(set_names)]
            ]
            conditions = dict(zip(where_names, params[len(set_names) :], strict=True))
            updated = 0
            for row in self.srcnet.get(table, {}).values():
                if all(row.get(k) == v for k, v in conditions.items()):
                    row.update(zip(set_names, set_values, strict=True))
                    updated += 1
            return FakeResult(rowcount=updated)

        match = re.match(r"INSERT INTO (srcnet\.\S+) \(([^)]*)\) VALUES", text)
        if match and "ON CONFLICT" in text:  # srcnet upsert
            table, columns = match.group(1), [c.strip() for c in match.group(2).split(",")]
            pk = re.search(r"ON CONFLICT \(([^)]*)\)", text).group(1)
            pk_columns = [c.strip() for c in pk.split(",")]
            values = [getattr(p, "obj", None) if type(p).__name__ == "Jsonb" else p for p in params]
            row = dict(zip(columns, values, strict=True))
            key = tuple(row[c] for c in pk_columns)
            self.srcnet.setdefault(table, {})[key] = row
            return FakeResult(rowcount=1)

        if text.startswith("SELECT to_jsonb(t) FROM"):  # srcnet fetch
            table = text.split()[3]
            keys = re.findall(r"(\w+) = %s", text)
            values = dict(zip(keys, params, strict=True))
            rows = [
                (dict(r),)
                for r in self.srcnet.get(table, {}).values()
                if all(r.get(k) == v for k, v in values.items())
            ]
            return FakeResult(rows)

        if re.search(r"FROM \S+ p\s+ORDER BY p.project_id", text):  # notifications listing
            projects = next(
                (rows for table, rows in self.srcnet.items() if table.endswith(".projects")),
                {},
            )
            listing = []
            for row in projects.values():
                counts = []
                for suffix in (".data_products", ".artifacts"):
                    table = next((r for t, r in self.srcnet.items() if t.endswith(suffix)), {})
                    counts.append(
                        sum(1 for r in table.values() if r.get("project_id") == row["project_id"])
                    )
                listing.append(
                    (row["project_id"], row.get("project_title"), row.get("data_rights"), *counts)
                )
            return FakeResult(sorted(listing))

        # DDL and TAP_SCHEMA registration during srcnet bootstrap
        return FakeResult()


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(tapcore.db, "_pool", FakePool(db))
    return db


@pytest.fixture
def results_dir(tmp_path):
    """Point settings.results_dir at a temp dir (Settings is frozen)."""
    from tapcore.config import settings

    original = settings.results_dir
    object.__setattr__(settings, "results_dir", str(tmp_path))
    yield str(tmp_path)
    object.__setattr__(settings, "results_dir", original)


@pytest.fixture
def client(fake_db, results_dir):
    """TestClient over the full app (lifespan runs srcnet bootstrap on the fake)."""
    from fastapi.testclient import TestClient
    from tap_api.main import app

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
