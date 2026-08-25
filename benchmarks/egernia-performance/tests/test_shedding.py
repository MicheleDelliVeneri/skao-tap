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

from egernia_bench import cluster  # noqa: E402
from egernia_bench.orchestrate import runner  # noqa: E402


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
    assert open512["transport_drops"] == 800
    assert open512["other_errors"] == {}
    limited = next(r for r in rows if r["key"].startswith("shed-D1-n1-limit"))
    assert limited["transport_drops"] == 0
    assert limited["other_errors"] == {"ReadTimeout": 3}


def test_every_transport_drop_counts_but_a_timeout_does_not():
    """A reset arrives as one of four exception classes depending on where in
    the exchange the socket died, so counting only ReadError understates the
    drops the package's "zero drops" claim is about. A ReadTimeout is not a
    drop: the connection was held and the answer was late."""
    by_type = dict.fromkeys(runner.TRANSPORT_DROP_ERRORS, 7) | {"ReadTimeout": 5}
    rows = runner.shedding_summary(
        [_fake("shed-D1-n1-open-c2048", 1, 2048, 9000, {"503": 10}, by_type)]
    )
    assert rows[0]["transport_drops"] == 7 * len(runner.TRANSPORT_DROP_ERRORS)
    assert rows[0]["other_errors"] == {"ReadTimeout": 5}
    assert "ConnectError" in runner.TRANSPORT_DROP_ERRORS
    assert not any("Timeout" in name for name in runner.TRANSPORT_DROP_ERRORS)


def test_a_connect_error_alone_is_a_drop():
    rows = runner.shedding_summary(
        [_fake("shed-D1-n2-open-c1024", 2, 1024, 5000, {}, {"ConnectError": 42})]
    )
    assert rows[0]["transport_drops"] == 42
    assert rows[0]["other_errors"] == {}


# ---------------------------------------------------------------------------
# The sharded closed loop (package 14's generator fix)
# ---------------------------------------------------------------------------


def test_sharded_loop_splits_concurrency_and_merges_shares(monkeypatch):
    """Three processes, seven clients: shares 3/2/2 with distinct seeds, one
    merged recorder — and the busiest process judged against one core."""
    from egernia_bench.load import runner as load_mod

    seen = []

    async def fake_closed_loop(base_url, workload, concurrency, warmup_s, measure_s, **kwargs):
        seen.append((workload.seed, concurrency))
        recorder = load_mod.Recorder()
        for i in range(concurrency):
            recorder.add(
                load_mod.Sample(
                    t_start=1.0,
                    t_offered=1.0,
                    query_class="Q01",
                    query_id=f"q{i}",
                    status=200,
                    error="",
                    latency_s=0.01,
                    ttfb_s=0.01,
                    response_bytes=10,
                    rows=-1,
                    pod="",
                    mode="sync",
                    request_id="",
                )
            )
        recorder.cpu_samples.append((1.0, 0.5, 0.1))
        return recorder, float(measure_s)

    monkeypatch.setattr(load_mod, "closed_loop", fake_closed_loop)
    monkeypatch.setattr(load_mod.Workload, "__init__", _workload_init_recording_seed)
    merged, elapsed = load_mod.closed_loop_sharded(
        "http://example",
        entries=[],
        mix={"Q01": 1.0},
        query_class=None,
        seed=1000,
        concurrency=7,
        warmup_s=0,
        measure_s=5,
        processes=3,
    )
    assert len(merged.samples) == 7
    assert merged.generator_cpu_peak == 0.5  # busiest process, one-core budget
    assert elapsed == 5.0


def _workload_init_recording_seed(self, entries, mix, seed):
    self.seed = seed


def test_generator_cpu_peak_is_a_fraction_of_one_core():
    """The bug this pins: dividing by the host's core count read a loop
    pinned at 100% of its core as 3% on a 30-core host, and the headroom
    guard stayed green while the sweep measured its own client."""
    from egernia_bench.load import runner as load_mod

    recorder = load_mod.Recorder()
    recorder.cpu_samples.append((0.0, 1.0, 0.2))  # process at 100% of a core
    assert recorder.generator_cpu_peak == 1.0
