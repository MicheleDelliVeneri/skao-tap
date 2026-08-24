"""Tests for the shedding family (package 13).

The family's claim is a comparison: the same held overload, refused with
503s under `limitConcurrency` where it was dropped at the socket without it.
For that to hold, every point on the ladder must replay the same workload,
the reduction must separate answers from drops without inventing either, and
the ceiling flip must go through the chart so the values file stays the
authority on what was deployed.
"""

import inspect
import pathlib
import sys

import yaml

SUITE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE))

from tapbench import cluster  # noqa: E402
from tapbench.orchestrate import runner  # noqa: E402


def _config():
    return yaml.safe_load((SUITE / "config/scenarios.yaml").read_text())


def test_config_is_a_ladder_bracketing_the_backlog():
    plan = _config()["shedding"]
    ladder = plan["held_concurrency"]
    assert ladder == sorted(ladder), "the ladder must climb"
    assert ladder[0] < 2048 <= ladder[-1], (
        "the ladder must bracket the default accept backlog (2048): the "
        "hypothesis under test is the listen queue overflowing"
    )
    assert 0 < plan["limit_concurrency"] < ladder[-1]
    assert plan["replicas"] == [1, 2], "the package's resolution names one and two replicas"


def test_each_ladder_point_replays_the_same_workload():
    """A fresh Workload per measurement, seeded by replicas only — so two
    points on the same ladder differ in held concurrency and nothing else."""
    source = inspect.getsource(runner.shedding)
    assert source.count("Workload(") == 1
    assert "seed=9000 + replicas" in source


def test_limit_flip_goes_through_the_chart(monkeypatch):
    calls = []
    monkeypatch.setattr(cluster, "install_chart", lambda overrides=None: calls.append(overrides))
    cluster.set_limit_concurrency(64)
    cluster.set_limit_concurrency(0)
    assert calls == [
        {"tapApi.limitConcurrency": "64"},
        {"tapApi.limitConcurrency": "0"},
    ]


def _fake(key, replicas, concurrency, requests, by_status, by_type):
    return {
        "kind": "shedding",
        "key": key,
        "replicas": replicas,
        "concurrency": concurrency,
        "http": {
            "requests": requests,
            "rps": requests / 45.0,
            "errors_by_status": by_status,
            "errors_by_type": by_type,
        },
    }


def test_summary_separates_answers_from_drops():
    results = [
        _fake("shed-D1-n1-open-c512", 1, 512, 9000, {"503": 1200}, {"ReadError": 800}),
        _fake("shed-D1-n1-open-c32", 1, 32, 4000, {}, {}),
        _fake(
            "shed-D1-n1-limit64-c512", 1, 512, 9500, {"503": 2100}, {"503": 2100, "ReadTimeout": 3}
        ),
        {"kind": "stress", "key": "not-mine", "http": {}},
    ]
    rows = runner.shedding_summary(results)
    assert [r["key"] for r in rows] == [
        "shed-D1-n1-limit64-c512",
        "shed-D1-n1-open-c32",
        "shed-D1-n1-open-c512",
    ]
    open512 = next(r for r in rows if r["key"] == "shed-D1-n1-open-c512")
    assert open512["refused_503"] == 1200
    assert open512["reset_readerror"] == 800
    assert open512["other_errors"] == {}
    limited = next(r for r in rows if r["key"].startswith("shed-D1-n1-limit"))
    assert limited["reset_readerror"] == 0
    assert limited["other_errors"] == {"ReadTimeout": 3}
