"""Provenance on resume: written once, never overwritten, corpus pinned."""

import json

import pytest
from tap_compare import cli, runs


class _Entry:
    def as_dict(self):
        return {"query_class": "Q01", "query_id": "q01-000", "adql": "SELECT 1"}


def _record(run, sha, target=None, scenario_name="compare", scenario=None):
    cli._record_provenance(
        run,
        target or {"name": "egernia-local"},
        {"corpus": {"seed": 1}},
        sha,
        scenario_name,
        scenario or {"ladder": [1]},
        [_Entry()],
    )


def test_provenance_written_once_and_kept_on_resume(tmp_path):
    run = runs.Run(path=tmp_path, scenario="compare")
    _record(run, "aaa111")
    first = (tmp_path / "environment.json").read_text()
    _record(run, "aaa111")  # a resume with the same corpus changes nothing
    assert (tmp_path / "environment.json").read_text() == first
    assert json.loads(first)["corpus_sha256"] == "aaa111"


def test_resume_with_a_different_corpus_is_refused(tmp_path):
    run = runs.Run(path=tmp_path, scenario="compare")
    _record(run, "aaa111")
    with pytest.raises(SystemExit, match="corpus"):
        _record(run, "bbb222")


def test_resume_with_a_different_scenario_is_refused(tmp_path):
    run = runs.Run(path=tmp_path, scenario="compare")
    _record(run, "aaa111")
    with pytest.raises(SystemExit, match="scenario"):
        _record(run, "aaa111", scenario_name="compare-demo")
    with pytest.raises(SystemExit, match="scenario"):
        _record(run, "aaa111", scenario={"ladder": [1, 4]})


def test_resume_with_different_targets_is_refused(tmp_path):
    run = runs.Run(path=tmp_path, scenario="compare")
    _record(run, "aaa111")
    with pytest.raises(SystemExit, match="target"):
        _record(run, "aaa111", target={"name": "dachs-local"})
