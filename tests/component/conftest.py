"""Component-test fixtures: a dedicated PostgreSQL database initialized from
db/init/*.sql, plus the tap-api and tap-executor services as subprocesses.

Requires a reachable PostgreSQL server (and the psql client). Configure with:
    TAP_TEST_ADMIN_URL   admin connection URL used to (re)create the test DB
                         [default: postgresql://tap:tap@127.0.0.1:5432/postgres]
Tests are skipped automatically when the server is unreachable.
"""

import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time

import httpx
import psycopg
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEST_DB = "tap_component_test"

pytestmark = pytest.mark.component


def _admin_url() -> str:
    return os.environ.get("TAP_TEST_ADMIN_URL", "postgresql://tap:tap@127.0.0.1:5432/postgres")


def _test_db_url() -> str:
    base, _, _ = _admin_url().rpartition("/")
    return f"{base}/{TEST_DB}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _connect_admin_or_skip() -> psycopg.Connection:
    """Open the admin connection, skipping the component tests if the server
    is unreachable."""
    try:
        return psycopg.connect(_admin_url(), autocommit=True, connect_timeout=5)
    except Exception as exc:
        # what pytest.skip() raises, raised directly so the caller can rely on
        # this branch never returning
        raise pytest.skip.Exception(f"PostgreSQL not reachable for component tests: {exc}") from exc


@pytest.fixture(scope="session")
def database_url():
    admin = _connect_admin_or_skip()
    if shutil.which("psql") is None:
        pytest.skip("psql client not available for component tests")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    url = _test_db_url()
    for script in sorted((REPO_ROOT / "db" / "init").glob("*.sql")):
        subprocess.run(
            ["psql", url, "-q", "-v", "ON_ERROR_STOP=1", "-f", str(script)],
            check=True,
            capture_output=True,
        )
    return url


@pytest.fixture(scope="session")
def tap_service(database_url, tmp_path_factory):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}/tap"
    results_dir = tmp_path_factory.mktemp("results")
    env = {
        **os.environ,
        "TAP_DATABASE_URL": database_url,
        "TAP_BASE_URL": base_url,
        "TAP_RESULTS_DIR": str(results_dir),
        "TAP_DEFAULT_MAXREC": "10000",
        "TAP_SYNC_TIMEOUT": "10",
    }
    # fixed location so CI can dump the logs on failure (pytest swallows
    # session-fixture teardown output)
    logs_dir = REPO_ROOT / ".service-logs"
    logs_dir.mkdir(exist_ok=True)
    with (
        open(logs_dir / "tap-api.log", "wb") as api_log,
        open(logs_dir / "tap-executor.log", "wb") as executor_log,
    ):
        api = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tap_api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env=env,
            cwd=REPO_ROOT,
            stdout=api_log,
            stderr=subprocess.STDOUT,
        )
        executor = subprocess.Popen(
            [sys.executable, "-m", "tap_executor.worker"],
            env=env,
            cwd=REPO_ROOT,
            stdout=executor_log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if httpx.get(f"{base_url}/availability", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass  # connection refused while the service boots: keep polling
                if time.monotonic() > deadline:
                    raise RuntimeError("tap-api did not become available")
                if api.poll() is not None or executor.poll() is not None:
                    raise RuntimeError("a service process exited during startup")
                time.sleep(0.3)
            yield base_url
        finally:
            for proc in (api, executor):
                proc.terminate()
            for proc in (api, executor):
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
