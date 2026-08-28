"""`demo/requirements.txt` and the `demo` dependency group must agree.

The demo is consumed two ways and only one of them can see `pyproject.toml`:

- locally, `make notebook` runs it out of this repository's `.venv`, which
  `uv sync --group demo` populated from the dependency group;
- deployed, the stack's `sync-notebooks` copies `demo/` into the JupyterHub
  volume, where this repository is not installed and `requirements.txt` is the
  only manifest that travelled with the files.

So the two lists are the same list, written twice because no single format is
readable by both. Drift between them does not fail anywhere near itself: it
surfaces as a missing module in someone's notebook, in a different repository,
possibly weeks later. That happened three times in one afternoon, which is why
this test exists rather than a comment asking people to remember.
"""

from __future__ import annotations

import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _requirement_names(lines: list[str]) -> set[str]:
    """Distribution names only, lowercased, comments and blanks dropped.

    Names rather than full specifiers on purpose: the floors are allowed to
    differ if someone has a reason (the notebook image may need a newer one
    than a local checkout does), but the *set of packages* may not, because
    that is what determines whether an import succeeds.
    """
    names = set()
    for line in lines:
        requirement = line.split("#", 1)[0].strip()
        if not requirement:
            continue
        for separator in (">=", "==", "<=", "~=", ">", "<", "[", ";"):
            requirement = requirement.split(separator, 1)[0]
        names.add(requirement.strip().lower())
    return names


def test_the_demo_requirements_match_the_dependency_group():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    group = _requirement_names(pyproject["dependency-groups"]["demo"])
    shipped = _requirement_names((ROOT / "demo" / "requirements.txt").read_text().splitlines())

    assert shipped == group, (
        "demo/requirements.txt and pyproject.toml's `demo` group disagree.\n"
        f"  only in the group:        {sorted(group - shipped) or 'none'}\n"
        f"  only in requirements.txt: {sorted(shipped - group) or 'none'}\n"
        "The group feeds `uv sync` for a local `make notebook`; the file is the "
        "only manifest that reaches the JupyterHub volume, because the stack's "
        "sync-notebooks copies demo/ and nothing else. Both need the package."
    )


def test_the_requirements_file_is_not_accidentally_empty():
    """A file of nothing but comments would pass the comparison only if the
    group were empty too, but it would also install nothing while looking
    maintained. Cheap to rule out."""
    shipped = _requirement_names((ROOT / "demo" / "requirements.txt").read_text().splitlines())
    assert len(shipped) >= 5, f"only {len(shipped)} requirements pinned; expected the demo stack"
