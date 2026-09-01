"""Startup schema handling: DDL by default, verify-only when disabled."""

import pytest
from egernia_core import bootstrap
from egernia_core.config import settings


@pytest.fixture
def startup_ddl_disabled():
    object.__setattr__(settings, "schema_bootstrap_on_startup", False)
    yield
    object.__setattr__(settings, "schema_bootstrap_on_startup", True)


def test_startup_runs_job_columns_ddl_by_default(fake_db):
    bootstrap.startup([], attempts=1)
    assert any(s.startswith("ALTER TABLE uws.jobs") for s in fake_db.statements)


def test_startup_disabled_verifies_without_ddl(fake_db, startup_ddl_disabled):
    bootstrap.startup([], attempts=1)
    assert not any("ALTER TABLE" in s or "CREATE " in s for s in fake_db.statements)
    assert any("FROM uws.jobs WHERE false" in s for s in fake_db.statements)


def test_check_ready_names_the_bootstrap_command():
    class BrokenConn:
        def execute(self, sql, params=None):
            raise Exception('relation "uws.jobs" does not exist')

    with pytest.raises(RuntimeError, match=r"python -m egernia_core\.bootstrap"):
        bootstrap.check_ready(BrokenConn(), [])


def test_cli_bootstrap_always_runs_ddl(fake_db, startup_ddl_disabled):
    """The explicit command exists to run the DDL, so it must ignore the
    flag that turns startup DDL off."""
    bootstrap.main()
    assert any(s.startswith("ALTER TABLE uws.jobs") for s in fake_db.statements)
    assert fake_db.closed
