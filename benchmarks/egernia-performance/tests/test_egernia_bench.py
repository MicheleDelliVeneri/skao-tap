"""Tests for the benchmark suite's own claims.

The suite makes four promises that a reader has to be able to rely on:
determinism, correct statistics, honest bottleneck rules, and timings that are
absent rather than guessed. Each is tested here, because a benchmark whose
harness is wrong produces confident numbers about nothing.
"""

import json
import math
import pathlib
import sys
import types

import pytest
import yaml

SUITE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE))

from egernia_bench import corpus  # noqa: E402
from egernia_bench.analyze import bottleneck, keda, stats  # noqa: E402
from egernia_bench.load import runner as load_runner  # noqa: E402


@pytest.fixture(scope="module")
def cfg():
    return {
        "scenarios": yaml.safe_load((SUITE / "config/scenarios.yaml").read_text()),
        "datasets": yaml.safe_load((SUITE / "config/datasets.yaml").read_text()),
    }


# -- determinism ------------------------------------------------------------


def test_the_corpus_is_identical_across_builds(cfg):
    """Two runs have to issue the same queries to be comparable at all."""
    first = corpus.build(cfg["scenarios"], cfg["datasets"], observations=100_000)
    second = corpus.build(cfg["scenarios"], cfg["datasets"], observations=100_000)
    assert corpus.corpus_hash(first) == corpus.corpus_hash(second)
    assert [e.adql for e in first] == [e.adql for e in second]


def test_the_corpus_hash_changes_when_the_corpus_does(cfg):
    """Otherwise the hash in the provenance would not be evidence of anything."""
    base = corpus.build(cfg["scenarios"], cfg["datasets"], observations=100_000)
    altered = dict(cfg["scenarios"])
    altered["corpus"] = {**cfg["scenarios"]["corpus"], "seed": 999}
    other = corpus.build(altered, cfg["datasets"], observations=100_000)
    assert corpus.corpus_hash(base) != corpus.corpus_hash(other)


def test_the_corpus_meets_the_required_size(cfg):
    entries = corpus.build(cfg["scenarios"], cfg["datasets"], observations=100_000)
    assert len(entries) >= 10_000
    assert len({e.adql for e in entries}) == len(entries), "combinations must be distinct"


def test_every_class_is_represented(cfg):
    grouped = corpus.by_class(corpus.build(cfg["scenarios"], cfg["datasets"], observations=100_000))
    assert set(grouped) == set(corpus.CLASSES)


def test_cone_searches_do_not_repeat_one_coordinate(cfg):
    """A corpus that queries one position measures the page cache."""
    grouped = corpus.by_class(corpus.build(cfg["scenarios"], cfg["datasets"], observations=100_000))
    centres = {(e.adql.split("CIRCLE")[1][:40]) for e in grouped["Q05"]}
    assert len(centres) > 1000


def test_the_python_prng_matches_the_generators_contract():
    """corpus.rnd reimplements bench.rnd; if it drifts, the corpus stops
    aiming at the data. The property pinned here is the one the SQL relies on:
    a masked 32-bit slice of the md5, scaled into [0, 1)."""
    import hashlib

    digest = hashlib.md5(b"7:42:ra").hexdigest()
    expected = (int(digest[:8], 16) & 0x7FFFFFFF) / 2147483648.0
    assert corpus.rnd("7", 42, "ra") == expected
    assert 0.0 <= corpus.rnd("7", 42, "ra") < 1.0


def test_declination_is_uniform_over_the_sphere():
    """Uniform in degrees would crowd the poles, and then a cone search's
    yield would depend mostly on where the corpus happened to point."""
    decs = [corpus.object_position("1", i)[1] for i in range(1, 20_000)]
    northern = sum(1 for d in decs if d > 0)
    assert 0.45 < northern / len(decs) < 0.55
    # Half the sphere's area lies within +/-30 degrees of the equator.
    equatorial = sum(1 for d in decs if abs(d) <= 30)
    assert 0.45 < equatorial / len(decs) < 0.55


def test_the_mix_is_drawn_deterministically(cfg):
    entries = corpus.build(cfg["scenarios"], cfg["datasets"], observations=10_000)
    mix = cfg["scenarios"]["query_mix"]["normal"]
    first = load_runner.Workload(entries, mix, seed=7)
    second = load_runner.Workload(entries, mix, seed=7)
    assert [first.next().query_id for _ in range(500)] == [
        second.next().query_id for _ in range(500)
    ]


def test_the_mix_follows_its_weights(cfg):
    entries = corpus.build(cfg["scenarios"], cfg["datasets"], observations=10_000)
    mix = cfg["scenarios"]["query_mix"]["normal"]
    workload = load_runner.Workload(entries, mix, seed=11)
    counts: dict[str, int] = {}
    for _ in range(20_000):
        cls = workload.next().query_class
        counts[cls] = counts.get(cls, 0) + 1
    for cls, weight in mix.items():
        assert abs(counts.get(cls, 0) / 20_000 - weight) < 0.02, cls


def test_a_mix_naming_an_absent_class_is_refused(cfg):
    entries = corpus.build(cfg["scenarios"], cfg["datasets"], observations=1000)
    with pytest.raises(ValueError):
        load_runner.Workload(entries, {"Q99": 1.0}, seed=1)


# -- statistics -------------------------------------------------------------


def _sample(latency, status=200, error="", bytes_=1000, offered=0.0, t=1000.0):
    return load_runner.Sample(
        t_start=t,
        t_offered=offered,
        query_class="Q05",
        query_id="x",
        status=status,
        error=error,
        latency_s=latency,
        ttfb_s=latency / 2,
        response_bytes=bytes_,
        rows=-1,
        pod="",
        mode="sync",
        request_id="",
    )


def test_percentiles_and_moments():
    samples = [_sample(i / 1000) for i in range(1, 1001)]
    summary = stats.summarise(samples, window_seconds=10.0)
    assert summary["requests"] == 1000
    assert summary["rps"] == 100.0
    assert summary["latency"]["p50_s"] == pytest.approx(0.5, abs=0.01)
    assert summary["latency"]["p95_s"] == pytest.approx(0.95, abs=0.01)
    assert summary["latency"]["p99_9_s"] == pytest.approx(1.0, abs=0.01)
    assert summary["latency"]["coefficient_of_variation"] == pytest.approx(
        summary["latency"]["stddev_s"] / summary["latency"]["mean_s"]
    )


def test_errors_are_counted_by_type_and_status():
    samples = (
        [_sample(0.1)] * 90
        + [_sample(0.1, status=503)] * 8
        + [_sample(0.1, status=0, error="ReadTimeout")] * 2
    )
    summary = stats.summarise(samples, window_seconds=1.0)
    assert summary["successful"] == 90
    assert summary["error_fraction"] == pytest.approx(0.10)
    assert summary["errors_by_status"]["503"] == 8
    assert summary["errors_by_type"]["ReadTimeout"] == 2
    assert summary["timeout_count"] == 2


def test_failed_requests_do_not_flatter_the_successful_percentiles():
    """A run where failures return instantly would otherwise report a p95 that
    belongs to the failures."""
    samples = [_sample(1.0)] * 50 + [_sample(0.001, status=503)] * 50
    summary = stats.summarise(samples, window_seconds=1.0)
    assert summary["latency"]["p50_s"] < summary["latency_successful"]["p50_s"]


def test_one_measurement_reports_no_interval():
    """+/- 0 would read as perfect precision."""
    single = stats.mean_ci([42.0])
    assert single["mean"] == 42.0
    assert single["ci95_low"] is None


def test_the_interval_uses_students_t_for_small_samples():
    values = [100.0, 110.0, 90.0]
    ci = stats.mean_ci(values)
    # t(2) = 4.303, not 1.96: with three samples the normal approximation is
    # about half as wide as it should be.
    expected_half = 4.303 * ci["stddev"] / math.sqrt(3)
    assert ci["ci95_high"] - ci["mean"] == pytest.approx(expected_half)


def test_the_bootstrap_interval_is_reproducible():
    import numpy as np

    values = np.random.default_rng(0).lognormal(size=500)
    first = stats.bootstrap_ci(values, lambda a, axis: np.percentile(a, 95, axis=axis))
    second = stats.bootstrap_ci(values, lambda a, axis: np.percentile(a, 95, axis=axis))
    assert first == second
    assert first[0] < first[1]


def test_coordinated_omission_is_measured_not_absorbed():
    """An open-loop generator that fell behind has to say so."""
    samples = [_sample(0.1, offered=999.0, t=1000.0) for _ in range(10)]
    omission = stats.coordinated_omission(samples)
    assert omission["mean_lateness_s"] == pytest.approx(1.0)
    assert omission["fraction_late_over_100ms"] == 1.0


def test_saturation_needs_more_than_one_signal_to_be_believed():
    baseline = {"latency": {"p95_s": 0.1}, "rps": 100.0, "error_fraction": 0.0}
    current = {"latency": {"p95_s": 0.6}, "rps": 101.0, "error_fraction": 0.0}
    thresholds = {
        "throughput_gain_below_fraction": 0.05,
        "p95_multiple_of_baseline": 5.0,
        "error_rate_above_fraction": 0.01,
        "tap_cpu_above_fraction": 0.95,
        "postgres_cpu_above_fraction": 0.95,
    }
    signals = stats.saturation_signals(current, baseline, baseline, {}, thresholds)
    assert set(signals["tripped"]) == {"throughput_plateau", "latency_blown"}
    assert signals["count"] == 2


# -- bottleneck rules -------------------------------------------------------


def _rows(metric, values):
    return [
        {"metric": metric, "labels": "", "t": float(i), "value": v} for i, v in enumerate(values)
    ]


def test_a_pinned_worker_is_cpu_bound_against_the_gil_ceiling_not_the_pod_limit():
    """The bug this pins: comparing 0.95 cores against a 2-core pod limit
    reported a fully pinned single-worker process as having headroom."""
    verdicts = bottleneck.classify(
        metrics_rows=_rows("tap_api_cpu_cores", [0.95] * 50),
        summary={"window_seconds": 60.0, "requests": 100, "error_fraction": 0.0},
        pg_summary={"cache_hit_ratio": 1.0},
        recorder_cpu_peak=0.05,
        limits={"tap_api_cpu_limit_cores": 1.0, "postgres_cpu_limit_cores": 4.0},
    )
    assert verdicts[0].classification == "TAP_CPU_BOUND"


def test_a_saturated_generator_outranks_everything():
    verdicts = bottleneck.classify(
        metrics_rows=_rows("tap_api_cpu_cores", [1.0] * 50),
        summary={"window_seconds": 60.0, "requests": 100, "error_fraction": 0.0},
        pg_summary={"cache_hit_ratio": 1.0},
        recorder_cpu_peak=0.95,
        limits={"tap_api_cpu_limit_cores": 1.0},
    )
    assert verdicts[0].classification == "LOAD_GENERATOR_BOUND"


def test_a_run_that_mostly_failed_says_so_rather_than_nothing_saturated():
    verdicts = bottleneck.classify(
        metrics_rows=_rows("tap_api_cpu_cores", [0.01] * 50),
        summary={
            "window_seconds": 60.0,
            "requests": 100,
            "error_fraction": 0.96,
            "errors_by_type": {"500": 96},
        },
        pg_summary={"cache_hit_ratio": 1.0},
        recorder_cpu_peak=0.01,
        limits={"tap_api_cpu_limit_cores": 1.0},
    )
    assert verdicts[0].classification == "UNKNOWN"
    assert "failed" in verdicts[0].explanation
    assert verdicts[0].evidence["error_fraction"] == 0.96


def test_a_poor_cache_hit_ratio_is_io_bound():
    verdicts = bottleneck.classify(
        metrics_rows=_rows("postgres_cpu_cores", [0.5] * 50),
        summary={"window_seconds": 60.0, "requests": 100, "error_fraction": 0.0},
        pg_summary={"cache_hit_ratio": 0.80, "blocks_read": 10_000_000},
        recorder_cpu_peak=0.05,
        limits={"tap_api_cpu_limit_cores": 1.0, "postgres_cpu_limit_cores": 4.0},
    )
    assert "DATABASE_IO_BOUND" in [v.classification for v in verdicts]


def test_every_rule_reports_the_numbers_that_made_it_fire():
    verdicts = bottleneck.classify(
        metrics_rows=_rows("tap_api_cpu_cores", [1.0] * 50),
        summary={"window_seconds": 60.0, "requests": 100, "error_fraction": 0.0},
        pg_summary={"cache_hit_ratio": 1.0},
        recorder_cpu_peak=0.05,
        limits={"tap_api_cpu_limit_cores": 1.0},
    )
    for verdict in verdicts:
        assert verdict.evidence, verdict.classification
        assert verdict.explanation


# -- autoscaling timings ----------------------------------------------------


def test_a_stage_that_cannot_be_established_is_none_with_a_reason():
    """A guessed timing is a wrong answer that looks like evidence."""
    result = keda.timings(
        t0=1000.0,
        metrics_rows=[],
        watcher_samples=[],
        pod_timings=[],
        samples=[],
        deployment="egernia-tap-executor",
        threshold=60.0,
        slo_p95_s=2.0,
    )
    assert result["stamps"]["T1"] is None
    assert result["stamps"]["T2"] is None
    assert result["latencies_s"]["total_scale_out"] is None
    assert any("replica-count change" in note for note in result["notes"])


def test_the_stages_are_read_from_the_party_responsible_for_each():
    metrics = [{"metric": "keda_scaler_metrics_value", "labels": "", "t": 1010.0, "value": 90.0}]
    watcher = [
        {"t": 995.0, "deployments": {"egernia-tap-executor": {"spec_replicas": 1}}},
        {"t": 1015.0, "deployments": {"egernia-tap-executor": {"spec_replicas": 3}}},
    ]
    pods = [
        {
            "pod": "exec-new",
            "component": "tap-executor",
            "created": 1016.0,
            "scheduled": 1017.0,
            "container_started": 1019.0,
            "ready": 1024.0,
        }
    ]
    samples = [
        types.SimpleNamespace(t_start=1025.0, latency_s=0.2, status=200, error=""),
    ]
    result = keda.timings(
        t0=1000.0,
        metrics_rows=metrics,
        watcher_samples=watcher,
        pod_timings=pods,
        samples=samples,
        deployment="egernia-tap-executor",
        threshold=60.0,
        slo_p95_s=2.0,
    )
    assert result["latencies_s"]["detection"] == pytest.approx(10.0)
    assert result["latencies_s"]["hpa_decision"] == pytest.approx(5.0)
    assert result["latencies_s"]["pod_provisioning"] == pytest.approx(8.0)
    assert result["stamps"]["T7"] is not None
    assert "proxy" in result["t7_method"]


def test_scale_behaviour_counts_reversals_and_replica_seconds():
    watcher = [
        {"t": 0.0, "deployments": {"d": {"spec_replicas": 1, "ready": 1}}},
        {"t": 10.0, "deployments": {"d": {"spec_replicas": 4, "ready": 1}}},
        {"t": 20.0, "deployments": {"d": {"spec_replicas": 2, "ready": 2}}},
        {"t": 30.0, "deployments": {"d": {"spec_replicas": 6, "ready": 2}}},
    ]
    behaviour = keda.scale_behaviour(watcher, "d")
    assert behaviour["scale_events"] == 3
    assert behaviour["direction_reversals"] == 2
    assert behaviour["peak_replicas"] == 6
    # 1x10 + 4x10 + 2x10 = 70 replica-seconds
    assert behaviour["replica_seconds"] == pytest.approx(70.0)


def test_rolling_percentile_buckets_by_completion_not_by_start():
    """Bucketing by start time would credit a slow request to the moment the
    system was still healthy."""
    samples = [types.SimpleNamespace(t_start=0.0, latency_s=30.0, status=200, error="")] + [
        types.SimpleNamespace(t_start=0.0, latency_s=30.0 + i * 0.01, status=200, error="")
        for i in range(10)
    ]
    windows = keda.rolling_percentile(samples, 95, window_s=10.0)
    assert windows, "a completed request must land in a window"
    assert all(t >= 30.0 for t, _ in windows)


# -- open-loop shape --------------------------------------------------------


def test_a_ramp_step_interpolates_linearly():
    step = load_runner.Step(seconds=100.0, rate=10.0, rate_end=110.0)
    assert step.rate_at(0.0) == 10.0
    assert step.rate_at(50.0) == pytest.approx(60.0)
    assert step.rate_at(100.0) == pytest.approx(110.0)
    # Past the end it holds rather than extrapolating into nonsense.
    assert step.rate_at(200.0) == pytest.approx(110.0)


def test_a_flat_step_ignores_the_ramp():
    assert load_runner.Step(seconds=10.0, rate=5.0).rate_at(7.0) == 5.0


def test_publish_drops_host_identifying_fields(tmp_path):
    """Publishing is the step that makes a run public, so it de-identifies.

    Runs recorded before environment() stopped capturing hostname and user
    still have them on disk; republishing one of those must not put a
    contributor's workstation name on the docs site.
    """
    from egernia_bench.analyze import publish as publish_mod

    run_dir = tmp_path / "20260101T000000Z-deadbeef-smoke"
    (run_dir / "plots").mkdir(parents=True)
    (run_dir / "summary.json").write_text(json.dumps({"scenarios": {}, "generated_at": 0}))
    (run_dir / "environment.json").write_text(
        json.dumps(
            {
                "host": {
                    "hostname": "SOMEBODYS-LAPTOP.local",
                    "user": "somebody",
                    "platform": "macOS-26.6.2-arm64",
                    "cpu_count": 14,
                },
                "git": {"sha": "deadbeef"},
            }
        )
    )

    docs = tmp_path / "docs"
    publish_mod.publish(run_dir, docs_dir=docs)

    published = json.loads((docs / run_dir.name / "environment.json").read_text())
    assert "hostname" not in published["host"]
    assert "user" not in published["host"]
    # Everything that makes two runs comparable survives.
    assert published["host"]["platform"] == "macOS-26.6.2-arm64"
    assert published["host"]["cpu_count"] == 14
    assert published["git"]["sha"] == "deadbeef"


def test_every_expected_index_is_created_by_the_schema():
    """EXPECTED_INDEXES is the contract the plan flags assert against; an
    index the schema never creates makes the flag fire on every run — which
    is exactly how the cone-search expression index went missing while its
    explanatory comment sat right above the index block (package 16)."""
    from egernia_bench.collect import postgres as pg_mod

    schema = (SUITE / "egernia_bench/dataset/schema.sql").read_text()
    for index_name in set(pg_mod.EXPECTED_INDEXES.values()):
        assert f"CREATE INDEX IF NOT EXISTS {index_name}" in schema, index_name


def test_larger_tiers_declare_their_own_warmup():
    """The 60 s default demonstrably does not warm D3/D4 (the first
    repetition at each size was colder than the rest); the tiers whose
    working sets exceed memory say how long steady state takes."""
    from egernia_bench.orchestrate import runner as runner_mod

    cfg = runner_mod.load_config()
    assert runner_mod.warmup_for(cfg, "D1") is None
    assert runner_mod.warmup_for(cfg, "D3") == 300.0
    assert runner_mod.warmup_for(cfg, "D4") == 600.0
    assert runner_mod.warmup_for(cfg, "D4") > runner_mod.warmup_for(cfg, "D3")
