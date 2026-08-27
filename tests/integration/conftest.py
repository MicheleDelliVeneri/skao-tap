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
import fcntl
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

AAPI_SERVICE_URL = f"{AAPI_URL}/{AAPI_SERVICE_VERSION}"
TAP_URL = f"{EGERNIA_URL}/tap"
API_URL = f"{EGERNIA_URL}/api/v1"

# The service name registered with the Permissions API — the directory under
# its etc/permissions/<env>/ — and so also the audience the exchanged token
# carries. `science-metadata` without the suffix is not registered.
EGERNIA_SERVICE = "science-metadata-api"
EGERNIA_SERVICE_VERSION = "1"

# How long a full-table aggregate over the seeded dataset may take. Generous:
# the point of these is that the service answers, not how fast — the benchmark
# suite is where timings are measured and published.
QUERY_TIMEOUT_S = int(os.getenv("EGERNIA_QUERY_TIMEOUT_S", "180"))


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


def _mint_token() -> str:
    """Drive the AAPI device flow and exchange for this service's audience."""
    from ska_src_auth_api.client.integration import AuthenticationIntegrationClient

    logger.info("minting a token via the AAPI device flow at %s", AAPI_SERVICE_URL)
    with AuthenticationIntegrationClient(AAPI_SERVICE_URL, TEST_USER, TEST_USER_PASSWORD) as flow:
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
    """A science-metadata-api-audience access token for the seeded test user.

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
        if cache.exists():
            # suppress rather than try/except: with ruff targeting py314 the
            # formatter rewrites `except (A, B):` to PEP 758's unparenthesized
            # form, which the stack's Python 3.13 test image cannot parse. A
            # truncated or unreadable cache is just a cache miss either way.
            with contextlib.suppress(ValueError, OSError):
                cached = json.loads(cache.read_text())
                if cached.get("token") and time.time() < cached.get("expires_at", 0):
                    logger.info("reusing the token minted by another worker")
                    return cached["token"]
        minted = _mint_token()
        # Well inside a token's own lifetime, so a long suite re-mints rather
        # than carrying one that expires mid-run.
        cache.write_text(json.dumps({"token": minted, "expires_at": time.time() + 1800}))
        return minted


@pytest.fixture(scope="session")
def session(token: str) -> requests.Session:
    """An authenticated session. Every request in the suite goes through it."""
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


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
