"""The integration suite must parse on the Python the test pod actually runs.

This repository targets 3.14 — pyproject's `target-version = "py314"` — but the
deployment stack's shared test image is AlmaLinux 9 with python3.13, and it
mounts `tests/integration` in as files rather than installing it. So the one
directory in this repository that has to be readable by an older interpreter is
exactly the one nothing else checks.

Written after `ruff format` rewrote `except (ValueError, OSError):` into PEP
758's unparenthesized form, which is valid in 3.14 and a SyntaxError in 3.13.
`ruff check` accepted it, the local suite ran, and the test pod failed to load
the conftest at all — every test errored on an import, with nothing in this
repository able to tell.

`ast.parse` takes the feature version to parse against, which is the whole
check: no 3.13 interpreter needed.
"""

import ast
import pathlib

import pytest

# The oldest interpreter the suite has to load on. Raise this when the stack's
# test image does; do not raise it to make a failure go away.
TEST_POD_PYTHON = (3, 13)

INTEGRATION = pathlib.Path(__file__).resolve().parents[1] / "integration"
SOURCES = sorted(INTEGRATION.rglob("*.py"))


def test_there_is_something_to_check():
    """A glob that quietly matches nothing would make this file a no-op."""
    assert SOURCES, f"no python files under {INTEGRATION}"


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_the_integration_suite_parses_on_the_test_pods_python(source: pathlib.Path):
    major, minor = TEST_POD_PYTHON
    try:
        ast.parse(source.read_text(), filename=str(source), feature_version=TEST_POD_PYTHON)
    except SyntaxError as exc:
        pytest.fail(
            f"{source.name}:{exc.lineno} does not parse on Python {major}.{minor}, "
            f"which is what the deployment stack's test image runs: {exc.msg}. "
            "The suite is mounted into that pod as source, so this is a load "
            "failure there rather than a runtime one."
        )
