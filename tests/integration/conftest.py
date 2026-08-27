"""Integration test configuration for a deployed egernia.

Runs against a live service in the SRC integration environment, not against a
TestClient. Shaped like the data-management API's suite: plain environment
variables with in-cluster defaults, session-scoped fixtures, and a token minted
through the Authentication API's device flow.

Self-contained on purpose. The deployment stack mounts only `test.paths` into
the shared test pod, so nothing here may import from elsewhere in this
repository — which is why the device-flow exchange appears here as well as in
demo/auth.py. Both are the same two calls the other services' integration
clients wrap; egernia has no client package to put them in.

Only `requests` and the auth client are used, both already present in the
stack's test image, so egernia needs no entry in its test/pyproject.toml.
"""

from __future__ import annotations

import contextlib
import csv
import fcntl
import io
import json
import logging
import os
import pathlib
import sys
import time

import pytest
import requests

logger = logging.getLogger(__name__)

HERE = pathlib.Path(__file__).resolve().parent

# Reached by ingress from inside the cluster; the ingress is what applies the
# proxy timeouts and body size this service needs, so tests go through it
# rather than straight to the Service.
EGERNIA_URL = os.getenv("EGERNIA_URL", "http://egernia.test").rstrip("/")
AAPI_URL = os.getenv("AAPI_URL", "https://aapi.test/api").rstrip("/")
AAPI_SERVICE_VERSION = os.getenv("AAPI_SERVICE_VERSION", "v1")
TEST_USER = os.getenv("TEST_USER", "test1")
TEST_USER_PASSWORD = os.getenv("TEST_USER_PASSWORD", "test")
# A user holding roles/dev/user and not roles/dev/oper. Every other seeded IAM
# user holds oper, so without this one the suite can only prove the policy
# permits — never that it denies, which is the half worth proving.
TEST_READER_USER = os.getenv("TEST_READER_USER", "")
TEST_READER_PASSWORD = os.getenv("TEST_READER_PASSWORD", "test")

AAPI_SERVICE_URL = f"{AAPI_URL}/{AAPI_SERVICE_VERSION}"
TAP_URL = f"{EGERNIA_URL}/tap"
API_URL = f"{EGERNIA_URL}/api/v1"

# The policy egernia is authorised against in the Permissions API — the `name`
# inside etc/permissions/<env>/egernia/v1/, which is what PAPI resolves by, and
# also the audience the exchanged token carries. Not `science-metadata-api`:
# that policy names itself `science-metadata` and its routes are CAOM-shaped.
EGERNIA_SERVICE = "egernia"
EGERNIA_SERVICE_VERSION = "1"

# How long a full-table aggregate over the seeded dataset may take. Generous:
# the point of these is that the service answers, not how fast — the benchmark
# suite is where timings are measured and published.
QUERY_TIMEOUT_S = int(os.getenv("EGERNIA_QUERY_TIMEOUT_S", "180"))

# A safety net, not the mechanism. post-deploy-egernia waits for the seeding
# Job, so a deployment that reported success has data and this returns on its
# first poll. It exists for the case that ordering does not hold — a suite
# pointed at a deployment mid-seed, or a seeding job restarted underneath one —
# because the alternative is every data-dependent test failing on the service's
# own shedding ("all database connections are busy") behind an nginx 503, which
# says nothing about the code.
#
# Matches TARGET_PRODUCTS on the seeding Job. Lower it for a smaller seed.
EXPECTED_PRODUCTS = int(os.getenv("EGERNIA_EXPECTED_PRODUCTS", "30000"))
DATASET_WAIT_S = float(os.getenv("EGERNIA_DATASET_WAIT_S", "900"))
DATASET_POLL_S = float(os.getenv("EGERNIA_DATASET_POLL_S", "15"))
# Consecutive clean answers required after the rows arrive. The row target is
# reached well before the seeder is done: generation writes its last batch, then
# the four GiST footprint indexes are rebuilt and a VACUUM ANALYZE runs, and the
# service sheds throughout. Counting rows alone would release the suite into
# exactly the window it exists to wait out — measured at 339 products/s, the
# rows land some fifteen minutes before the database is calm again.
DATASET_SETTLE_POLLS = int(os.getenv("EGERNIA_DATASET_SETTLE_POLLS", "3"))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: exercises a deployed service, not a TestClient"
    )
    # Only when this suite is the one running. `force=True` removes the root
    # handlers pytest's caplog installs, so configuring it at import time broke
    # a unit test's log assertions in a full-suite run — this hook fires after
    # collection paths are known and is skipped when the suite is not enabled.
    if _enabled():
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True)


def _enabled() -> bool:
    return os.environ.get("EGERNIA_RUN_INTEGRATION_TESTS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip unless a caller opts in.

    `testpaths = ["tests"]` means a bare `pytest` collects this directory, and
    these tests need a deployment. Without the guard the default developer
    command fails on connection errors that say nothing about the code. The
    deployment stack's test job sets the variable.

    Filtered to this directory: pytest calls this hook once per session with
    every collected item, wherever the conftest defining it lives, so an
    unfiltered loop skips the unit and component suites too.
    """
    if _enabled():
        return
    skip = pytest.mark.skip(
        reason="set EGERNIA_RUN_INTEGRATION_TESTS=1 and point EGERNIA_URL at a deployment"
    )
    for item in items:
        if HERE in pathlib.Path(str(item.path)).parents:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def base_url() -> str:
    return EGERNIA_URL


@pytest.fixture(scope="session")
def tap_url() -> str:
    return TAP_URL


@pytest.fixture(scope="session")
def api_url() -> str:
    return API_URL


def _mint_token(username: str = "", password: str = "") -> str:
    """Drive the AAPI device flow and exchange for this service's audience."""
    from ska_src_auth_api.client.integration import AuthenticationIntegrationClient

    username = username or TEST_USER
    password = password or TEST_USER_PASSWORD
    logger.info("minting a token for %s via the AAPI device flow", username)
    with AuthenticationIntegrationClient(AAPI_SERVICE_URL, username, password) as flow:
        flow.authorize()
        raw = flow.fetch_token()["token"]["access_token"]
        # The device-flow token's audience is the Authentication API. egernia
        # checks for its own, so the exchange is not optional: without it the
        # token is valid and still refused.
        exchanged = flow.exchange_token(
            service=EGERNIA_SERVICE,
            version=EGERNIA_SERVICE_VERSION,
            access_token=raw,
        )
    return exchanged.json()["access_token"]


@pytest.fixture(scope="session")
def token() -> str:
    """An egernia-audience access token for the seeded test user.

    Minted once for the whole run, not once per worker. The stack's test
    entrypoint runs pytest with `-n auto`, and a session-scoped fixture is
    per-worker: on the integration cluster that meant two dozen workers each
    starting a headless browser against IAM at the same instant. One flock'd
    file makes the first worker mint and the rest read what it wrote.

    Env first, so a suite can be pointed at a deployment whose IAM this pod
    cannot drive a browser against.
    """
    for key in ("EGERNIA_TOKEN", "EGERNIA_TEST_TOKEN"):
        existing = os.environ.get(key, "").strip()
        if existing:
            logger.info("using %s from the environment", key)
            return existing

    cache = pathlib.Path(os.getenv("EGERNIA_TOKEN_CACHE", "/tmp/egernia-token.json"))
    lock = cache.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    # Held across the mint, not just the read: the point is that only one
    # browser ever runs, so the losers wait rather than racing.
    with open(lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        # Default to a miss, and bind before the read: a truncated or
        # unreadable cache is just a miss, and `cached` has to exist either
        # way for the checks below.
        #
        # suppress rather than try/except: with ruff targeting py314 the
        # formatter rewrites `except (A, B):` into PEP 758's unparenthesized
        # form, which the stack's Python 3.13 test image cannot parse.
        cached: dict = {}
        with contextlib.suppress(ValueError, OSError):
            cached = json.loads(cache.read_text())

        if not cached or time.time() >= cached.get("expires_at", 0):
            pass  # nothing usable; fall through and mint
        elif cached.get("token"):
            logger.info("reusing the token minted by another worker")
            return cached["token"]
        elif cached.get("error"):
            # A failure is cached too. Without this every worker in turn drove
            # its own browser against a broken IAM: the run that exposed it
            # took 8m56s to report the same error 24 times, and hammered IAM
            # into 500s on the way. The first failure decides for the run.
            pytest.fail(
                f"token minting already failed in this run: {cached['error']}",
                pytrace=False,
            )

        try:
            minted = _mint_token()
        except Exception as exc:
            cache.write_text(
                json.dumps(
                    {
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                        "expires_at": time.time() + 1800,
                    }
                )
            )
            raise
        # Well inside a token's own lifetime, so a long suite re-mints rather
        # than carrying one that expires mid-run.
        cache.write_text(json.dumps({"token": minted, "expires_at": time.time() + 1800}))
        return minted


@contextlib.contextmanager
def _locked_cache(path: pathlib.Path):
    """Serialise on `path`.lock and yield whatever `path` holds, as a dict.

    The same shape the token fixture uses: xdist gives every worker its own
    session-scoped fixtures, so anything that must happen once per run has to
    coordinate through the filesystem.
    """
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state: dict = {}
        with contextlib.suppress(ValueError, OSError):
            state = json.loads(path.read_text())
        yield state


@pytest.fixture(scope="session")
def dataset_ready(session: requests.Session) -> int:
    """Block until the post-deploy seeder has finished, and return the row count.

    Completion is "the expected number of data products is present", not "the
    Job says Complete": the suite has no cluster credentials, and the row count
    is what the tests actually depend on. A 503 while waiting is progress
    rather than a failure — it is the API shedding under the seeder's load,
    which is precisely what this waits out.

    One worker polls and publishes; the rest read what it wrote.
    """
    cache = pathlib.Path(os.getenv("EGERNIA_DATASET_CACHE", "/tmp/egernia-dataset.json"))
    with _locked_cache(cache) as state:
        if state.get("rows", 0) >= EXPECTED_PRODUCTS:
            return int(state["rows"])

        deadline = time.monotonic() + DATASET_WAIT_S
        rows = -1
        settled = 0
        while True:
            response = sync_query(session, "SELECT COUNT(*) AS n FROM srcnet.data_products")
            if response.status_code == 200:
                try:
                    rows = int(next(csv.DictReader(io.StringIO(response.text)))["n"])
                except (StopIteration, KeyError, ValueError):
                    rows = -1
                if rows >= EXPECTED_PRODUCTS:
                    settled += 1
                    if settled >= DATASET_SETTLE_POLLS:
                        cache.write_text(json.dumps({"rows": rows}))
                        logger.info("dataset ready: %d data products", rows)
                        return rows
                    logger.info(
                        "rows are in (%d); waiting for the load to settle (%d/%d)",
                        rows,
                        settled,
                        DATASET_SETTLE_POLLS,
                    )
                else:
                    settled = 0
                    logger.info(
                        "waiting for the seeder: %d/%d data products", rows, EXPECTED_PRODUCTS
                    )
            else:
                # 503 here is the API shedding while the seeder loads. The index
                # rebuild does that after every row has landed, so a shed resets
                # the streak rather than counting towards it.
                settled = 0
                logger.info("waiting for the seeder: HTTP %d", response.status_code)
            if time.monotonic() >= deadline:
                pytest.fail(
                    f"the dataset was still incomplete after {DATASET_WAIT_S:.0f}s"
                    f" ({rows} of {EXPECTED_PRODUCTS} data products). Check the seeding"
                    " job: kubectl logs -n egernia -l component=dataset-seed",
                    pytrace=False,
                )
            time.sleep(DATASET_POLL_S)


@pytest.fixture(scope="session")
def session(token: str) -> requests.Session:
    """An authenticated session. Every request in the suite goes through it."""
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


@pytest.fixture(scope="session")
def reader_session() -> requests.Session:
    """A session for a user with reads but no mutation grant.

    Skips rather than fails when TEST_READER_USER is unset, so a deployment
    whose IAM has not been reseeded with that user still runs the rest of the
    suite — the skip is the signal that the deny direction is unproven there.
    """
    if not TEST_READER_USER:
        pytest.skip(
            "TEST_READER_USER is unset: seed an IAM user with roles/dev/user and "
            "without roles/dev/oper to exercise the deny direction"
        )
    cache = pathlib.Path(os.getenv("EGERNIA_READER_TOKEN_CACHE", "/tmp/egernia-reader.json"))
    with _locked_cache(cache) as state:
        token = state.get("token", "")
        if not token or time.time() >= state.get("expires_at", 0):
            token = _mint_token(TEST_READER_USER, TEST_READER_PASSWORD)
            cache.write_text(json.dumps({"token": token, "expires_at": time.time() + 1800}))
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


@pytest.fixture
def timing(request: pytest.FixtureRequest):
    """Record a measurement so a run leaves a trend, not just a verdict.

    One JSON object per line, prefixed `TIMING`, because that is the only
    artefact that survives: the stack streams the pod's log and keeps nothing
    else, and pytest-xdist forwards worker logs to it. Extract a run with

        kubectl logs -n egernia -l component=integration-test \
          | sed -n 's/.*TIMING //p'

    Also attached as a JUnit property, for whenever the runner starts
    collecting the XML.
    """

    def record(label: str, seconds: float, budget: float) -> None:
        entry = {
            "test": request.node.name,
            "label": label,
            "seconds": round(seconds, 3),
            "budget": budget,
            "headroom": round(budget - seconds, 3),
        }
        logger.info("TIMING %s", json.dumps(entry, sort_keys=True))
        request.node.user_properties.append((f"timing.{label}", seconds))

    return record


@pytest.fixture(scope="session")
def anonymous() -> requests.Session:
    """An unauthenticated session, for asserting that the gate is closed."""
    return requests.Session()


def sync_query(session: requests.Session, adql: str, fmt: str = "csv") -> requests.Response:
    """POST an ADQL query to /tap/sync and return the raw response."""
    return session.post(
        f"{TAP_URL}/sync",
        data={"LANG": "ADQL", "QUERY": adql, "RESPONSEFORMAT": fmt},
        timeout=QUERY_TIMEOUT_S,
    )
