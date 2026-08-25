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


class FakeStreamCursor:
    """Cursor streaming the configured result set, psycopg-style: the query
    is sent (and any error raised) on the generator's first iteration, and
    ``description`` is populated with the first exchange."""

    def __init__(self, db):
        self._db = db
        self.description = None
        self.stream_chunk_rows = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._db.statements.append(sql.strip())
        if self._db.result_error is not None:
            raise self._db.result_error
        self.description = self._db.result_description

    def stream(self, sql, params=None, *, size=1):
        self.stream_chunk_rows = size
        self.execute(sql, params)
        yield from list(self._db.result_rows)


class FakeConnection:
    def __init__(self, db):
        self._db = db

    @contextlib.contextmanager
    def transaction(self):
        yield self

    def cursor(self, name=None):
        return FakeStreamCursor(self._db)

    def commit(self):
        pass

    def execute(self, sql, params=None):
        return self._db.run(sql, params or ())


class FakePool:
    def __init__(self, db):
        self._db = db

    @contextlib.contextmanager
    def connection(self, timeout: float | None = None):
        # Mirrors psycopg_pool's signature: the readiness probe asks for a
        # connection with its own short timeout, and a fake that refuses the
        # argument would make that path look broken when it is not.
        del timeout
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
        self.terminated: list[int] = []
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
        # result set produced by streamed result queries
        self.result_description = [FakeColumn("source_id", 20), FakeColumn("ra", 701)]
        self.result_rows = [(1, 62.1), (2, 62.2)]
        self.result_error: Exception | None = None
        # VOSI /tables metadata
        self.schemas = [("ska", "SKA continuum catalogues")]
        self.schema_tables = [("ska", "ska.continuum_sources", "table", "continuum sources", None)]
        self.schema_columns = [
            (
                "ska.continuum_sources",
                "source_id",
                "long",
                None,
                "source identifier",
                None,
                "meta.id",
                None,
                None,
            ),
            (
                "ska.continuum_sources",
                "ra",
                "double",
                None,
                "right ascension",
                "deg",
                "pos.eq.ra",
                None,
                None,
            ),
        ]
        # /api/v1/tables aggregate
        self.json_tables = [
            (
                "ska",
                "ska.continuum_sources",
                "continuum sources",
                None,
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

    def run(self, sql, params=()):
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

        if head.startswith("SELECT QUOTE_LITERAL"):
            # the obscore bootstrap quotes its view fingerprint server-side
            value = str(params[0]).replace("'", "''")
            return FakeResult([(f"'{value}'",)])

        if "pg_cancel_backend" in text:
            self.cancelled.append(params[0])
            return FakeResult([(True,)])

        if "pg_terminate_backend" in text:
            self.terminated.append(params[0])
            return FakeResult([(True,)])

        if "FROM pg_stat_activity" in text:
            return FakeResult()  # cancelled backend no longer active

        if text.startswith("SELECT phase, count(*) FROM uws.jobs"):
            counts: dict[str, int] = {}
            for job in self.jobs.values():
                counts[job["phase"]] = counts.get(job["phase"], 0) + 1
            return FakeResult(list(counts.items()))

        if text.startswith("SELECT extract(epoch FROM now() - min(creation_time))"):
            queued = [j["creation_time"] for j in self.jobs.values() if j["phase"] == "QUEUED"]
            if not queued:
                return FakeResult([(None,)])
            return FakeResult([((self._now - min(queued)).total_seconds(),)])

        if text.startswith("SELECT phase FROM uws.jobs"):
            job = self.jobs.get(params[0])
            return FakeResult([(job["phase"],)] if job else [])

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
            (
                job_id,
                run_id,
                owner_id,
                created,
                duration,
                destruction,
                parameters,
                request_id,
            ) = params
            self.jobs[job_id] = dict.fromkeys(JOB_KEYS) | {
                "job_id": job_id,
                "phase": "PENDING",
                "run_id": run_id,
                "owner_id": owner_id,
                "creation_time": created,
                "execution_duration": duration,
                "destruction": destruction,
                "parameters": json.loads(parameters),
                "request_id": request_id,
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

        if text.startswith("SELECT owner_id FROM uws.jobs WHERE job_id"):
            job = self.jobs.get(params[0])
            return FakeResult([(job["owner_id"],)] if job else [])

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
            if "owner_id IS NULL OR owner_id =" in text:  # ownership filter
                subject = params[index]
                jobs = [j for j in jobs if j["owner_id"] is None or j["owner_id"] == subject]
                index += 1
            jobs.sort(key=lambda j: j["creation_time"], reverse=True)
            if "LIMIT" in text:
                jobs = jobs[: params[index]]
            return FakeResult([_job_row(j) for j in jobs])

        if head.startswith("UPDATE UWS.JOBS SET"):
            match = re.match(
                r"UPDATE uws\.jobs SET (.*?) WHERE (.*?)(?: RETURNING (.*))?$", text, re.S
            )
            set_part, where_part, returning = match.groups()
            remaining = list(params)
            sets = {}
            for assignment in re.split(r",\s*(?![^(]*\))", set_part):
                name, _, rhs = assignment.partition("=")
                name, rhs = name.strip(), rhs.strip()
                if rhs == "%s":
                    sets[name] = remaining.pop(0)
                elif rhs.upper() == "NULL":
                    sets[name] = None
                else:
                    sets[name] = rhs.strip("'")
            conditions = []
            for cond in re.split(r"\s+AND\s+", where_part.strip()):
                name, _, rhs = cond.partition("=")
                name, rhs = name.strip(), rhs.strip()
                if rhs == "%s":
                    conditions.append((name, "eq", remaining.pop(0)))
                elif rhs.startswith("ANY(%s)"):
                    conditions.append((name, "in", remaining.pop(0)))
                else:
                    conditions.append((name, "eq", rhs.strip("'")))
            matched = [
                j
                for j in self.jobs.values()
                if all(
                    (j.get(n) in v) if op == "in" else (j.get(n) == v) for n, op, v in conditions
                )
            ]
            for job in matched:
                for name, value in sets.items():
                    job[name] = (
                        json.loads(value)
                        if name == "parameters" and isinstance(value, str)
                        else value
                    )
            if returning:
                cols = [c.strip() for c in returning.split(",")]
                return FakeResult([tuple(j[c] for c in cols) for j in matched])
            return FakeResult(rowcount=len(matched))

        if head.startswith("DELETE FROM UWS.JOBS WHERE DESTRUCTION"):
            now = datetime.datetime.now(datetime.UTC)
            expired = [j for j in self.jobs.values() if j["destruction"] < now]
            for job in expired:
                del self.jobs[job["job_id"]]
            return FakeResult([(j["job_id"],) for j in expired])

        if head.startswith("DELETE FROM UWS.JOBS"):
            return FakeResult(rowcount=1 if self.jobs.pop(params[0], None) else 0)

        match = re.match(r"DELETE FROM ((?:srcnet|software)\.\S+) WHERE (\w+) = %s", text)
        if match:  # metadata root delete; PostgreSQL cascades to descendants
            root_table, id_column = match.groups()
            root_rows = self.srcnet.get(root_table, {})
            root_keys = [key for key, row in root_rows.items() if row.get(id_column) == params[0]]
            for key in root_keys:
                del root_rows[key]
            if root_keys:
                for rows in self.srcnet.values():
                    descendant_keys = [
                        key for key, row in rows.items() if row.get(id_column) == params[0]
                    ]
                    for key in descendant_keys:
                        del rows[key]
            return FakeResult(rowcount=len(root_keys))

        match = re.match(r"UPDATE ((?:srcnet|software)\.\S+) SET ", text)
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

        match = re.match(r"INSERT INTO ((?:srcnet|software)\.\S+) \(([^)]*)\) VALUES", text)
        if match and "ON CONFLICT" in text:  # srcnet upsert
            table, columns = match.group(1), [c.strip() for c in match.group(2).split(",")]
            pk_match = re.search(r"ON CONFLICT \(([^)]*)\)", text)
            assert pk_match is not None
            pk_columns = [c.strip() for c in pk_match.group(1).split(",")]
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

        if text.startswith("SELECT to_jsonb(p)"):  # generic plugin listing
            root_match = re.search(r"FROM (\S+) p ORDER BY p\.(\w+)", text)
            root, order_column = root_match.group(1), root_match.group(2)
            descendants = re.findall(r"FROM (\S+) c WHERE c\.(\w+) = p\.", text)
            listing = []
            for row in self.srcnet.get(root, {}).values():
                counts = tuple(
                    sum(
                        1 for r in self.srcnet.get(table, {}).values() if r.get(key) == row.get(key)
                    )
                    for table, key in descendants
                )
                listing.append((dict(row), *counts))
            listing.sort(key=lambda entry: str(entry[0].get(order_column)))
            return FakeResult(listing)

        # DDL and TAP_SCHEMA registration during srcnet bootstrap
        return FakeResult()


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(tapcore.db, "_pool", FakePool(db))
    return db


SOFTWARE_PAYLOAD = {
    "uri": "ska:dsc-037-delay-ps:0.1.3",
    "description": "Delay power-spectrum pipeline",
    "release_date": "2026-01-15T00:00:00Z",
    "status": "STABLE",
    "artifacts": [
        {
            "kind": "DOCKER",
            "location": "images.canfar.net/dsc-037-delay-ps:0.1.3",
            "cpu_architecture": ["amd64", "arm64"],
            "digest": "sha256:" + "ab" * 32,
            "supported_modes": ["HEADLESS"],
        }
    ],
    "discovery": {"science_category": ["EoR"], "tools_included": ["casa"]},
    "resources": {"requires_gpu": True, "min_memory": 16},
    "provenance": {"registered_by": "onyx", "registration_date": "2026-01-16T00:00:00Z"},
}


@pytest.fixture
def software_payload():
    """A valid ska-src-sdm document for the built-in software domain."""
    import copy

    return copy.deepcopy(SOFTWARE_PAYLOAD)


# --- stub INDIGO IAM -------------------------------------------------------
# Token verification is the security-critical path, so the tests sign real
# RS256 JWTs and serve a real JWKS from an in-process transport rather than
# stubbing the verifier out.

IAM_ISSUER = "https://iam.example.org"
IAM_AUDIENCE = "science-metadata"


def _rsa_jwk(kid="k1"):
    import json as _json

    import jwt as _jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = _json.loads(_jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private, jwk


@pytest.fixture(scope="session")
def iam_issuer():
    return IAM_ISSUER


@pytest.fixture(scope="session")
def iam_audience():
    return IAM_AUDIENCE


@pytest.fixture(scope="session")
def iam_keypair():
    """The IAM's signing key: tokens signed with it verify."""
    return _rsa_jwk()


@pytest.fixture(scope="session")
def forged_keypair():
    """A different key under the same kid: tokens signed with it must not."""
    return _rsa_jwk(kid="k1")


@pytest.fixture
def make_token(iam_keypair):
    """Mint a signed access token, overriding any claim."""
    import time as _time

    import jwt as _jwt

    def build(private=None, *, kid="k1", **overrides):
        claims = {
            "sub": "user-1",
            "iss": IAM_ISSUER,
            "aud": IAM_AUDIENCE,
            "exp": int(_time.time()) + 300,
            "iat": int(_time.time()),
            "groups": ["/ska/science-metadata/oper"],
            "scope": "openid profile science-metadata:write",
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return _jwt.encode(
            claims, private or iam_keypair[0], algorithm="RS256", headers={"kid": kid}
        )

    return build


@pytest.fixture
def stub_iam(monkeypatch, iam_keypair):
    """Serve the OIDC discovery document and JWKS in-process."""
    import urllib.request

    import httpx

    state = {"issuer": IAM_ISSUER, "keys": [iam_keypair[1]], "jwks_calls": 0}

    def handler(request):
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200, json={"issuer": state["issuer"], "jwks_uri": f"{IAM_ISSUER}/jwk"}
            )
        if request.url.path == "/jwk":
            state["jwks_calls"] += 1
            return httpx.Response(200, json={"keys": state["keys"]})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def patched_get(url, **kwargs):
        kwargs.pop("timeout", None)
        with real_client(transport=transport) as client:
            return client.get(url, **kwargs)

    def patched_urlopen(url, *args, **kwargs):
        target = url.full_url if hasattr(url, "full_url") else url
        with real_client(transport=transport) as client:
            body = client.get(target).content

        class _Response:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Response()

    monkeypatch.setattr(httpx, "get", patched_get)
    monkeypatch.setattr(urllib.request, "urlopen", patched_urlopen)

    from tapcore.auth import reset_verifier

    reset_verifier()
    yield state
    reset_verifier()


@pytest.fixture
def auth_settings(monkeypatch):
    """Override auth-related settings (Settings is frozen) and reset the caches."""
    from tapcore.auth import reset_verifier
    from tapcore.config import settings

    original = {}

    def apply(**overrides):
        for key, value in overrides.items():
            original.setdefault(key, getattr(settings, key))
            object.__setattr__(settings, key, value)
        reset_verifier()
        _reset_api_auth()

    yield apply

    for key, value in original.items():
        object.__setattr__(settings, key, value)
    reset_verifier()
    _reset_api_auth()


def _reset_api_auth():
    from tap_api.auth import reset_plugin

    reset_plugin()


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
