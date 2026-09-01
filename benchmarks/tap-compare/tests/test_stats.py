"""Statistics: the intervals and error accounting the comparison rests on."""

import math

import pytest
from tap_compare import runner as load_runner
from tap_compare import stats


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
