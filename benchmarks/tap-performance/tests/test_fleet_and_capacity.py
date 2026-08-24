"""Tests for what a multi-replica number means.

Every API series the classifier reads is summed over the pods, and every rate
the report divides is a rate somebody chose to offer. Both invite the same
mistake — comparing a fleet's total against one pod's ceiling, dividing two
rates neither of which is a limit — and both produce a confident wrong answer
rather than a missing one. These pin the corrections.
"""

import pathlib
import sys

SUITE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE))

from tapbench.analyze import bottleneck, keda  # noqa: E402
from tapbench.collect import guards  # noqa: E402
from tapbench.orchestrate import runner  # noqa: E402

LIMITS = {
    "tap_api_cpu_limit_cores": 1.0,
    "postgres_cpu_limit_cores": 4.0,
    "db_pool_max_total": 8,
    "tap_api_workers": 1,
    "tap_api_memory_limit_bytes": 1 << 30,
}


def _rows(*series):
    rows = []
    for metric, values in series:
        rows += [
            {"metric": metric, "labels": "", "t": float(i), "value": v}
            for i, v in enumerate(values)
        ]
    return rows


def _classify(rows, **kwargs):
    return bottleneck.classify(
        metrics_rows=rows,
        summary={"window_seconds": 60.0, "requests": 1000, "error_fraction": 0.0},
        pg_summary=kwargs.pop("pg_summary", {"cache_hit_ratio": 1.0}),
        recorder_cpu_peak=0.05,
        limits=LIMITS,
        **kwargs,
    )


# -- fleet ceilings ---------------------------------------------------------


def test_a_fleets_pool_ceiling_is_per_process_times_the_pods_serving():
    """The bug this pins: eight pods holding eight connections each were
    called pool-bound at 12 in use, because 12 exceeds one process's eight."""
    rows = _rows(
        ("tap_db_connections_in_use", [12.0] * 50),
        ("api_replicas_ready", [8.0] * 50),
    )
    verdicts = {v.classification: v for v in _classify(rows)}
    assert "CONNECTION_POOL_BOUND" not in verdicts

    # The same 12 connections against a single pod is genuinely full.
    alone = _rows(
        ("tap_db_connections_in_use", [12.0] * 50),
        ("api_replicas_ready", [1.0] * 50),
    )
    assert "CONNECTION_POOL_BOUND" in {v.classification for v in _classify(alone)}


def test_a_fleets_memory_ceiling_is_per_pod_times_the_pods_serving():
    """1.77 GB across eight pods is 220 MiB each, not a pod over its 1 GiB."""
    rows = _rows(
        ("tap_api_memory_bytes", [1.77e9] * 50),
        ("api_replicas_ready", [8.0] * 50),
    )
    assert "MEMORY_BOUND" not in {v.classification for v in _classify(rows)}

    crowded = _rows(
        ("tap_api_memory_bytes", [1.77e9] * 50),
        ("api_replicas_ready", [1.0] * 50),
    )
    assert "MEMORY_BOUND" in {v.classification for v in _classify(crowded)}


def test_the_multiplier_comes_from_the_run_not_the_configuration():
    """Under an autoscaler the configured count is not what served, so the
    ceilings follow the replicas that were actually ready."""
    assert bottleneck.fleet_replicas(_rows(("api_replicas_ready", [1, 3, 6, 6]))) == 6.0
    # No series, or an empty one, must not multiply a ceiling by zero.
    assert bottleneck.fleet_replicas([]) == 1.0
    assert bottleneck.fleet_replicas(_rows(("api_replicas_ready", [0.0]))) == 1.0


# -- the generator's own schedule -------------------------------------------


def test_arrivals_abandoned_at_the_cap_fail_a_guard_that_lateness_cannot_see():
    """The blind spot this pins: at the in-flight cap the generator drops the
    arrival instead of issuing it late, so lateness reads clean while nine
    tenths of the offered rate was never offered."""
    verdicts = {
        v.name: v
        for v in guards.schedule_verdicts(
            lateness_p95_s=0.2,
            lateness_max_s=1.0,
            arrivals_dropped=189581,
            arrivals_issued=31914,
        )
    }
    assert verdicts["load_generator_kept_schedule"].ok
    assert not verdicts["load_generator_offered_the_rate"].ok
    assert verdicts["load_generator_offered_the_rate"].measured["arrivals_dropped"] == 189581


def test_a_run_that_kept_its_schedule_passes_both():
    verdicts = guards.schedule_verdicts(
        lateness_p95_s=0.0, lateness_max_s=0.05, arrivals_dropped=0, arrivals_issued=27538
    )
    assert all(v.ok for v in verdicts)
    # Nothing dropped, so there is nothing to report about drops.
    assert [v.name for v in verdicts] == ["load_generator_kept_schedule"]


# -- capacities and the ratio of two of them --------------------------------


def _point(replicas, offered, rps, *, p95=0.02, errors=0.0, invalid=False):
    return {
        "key": f"repl-n{replicas}-{offered:g}",
        "kind": "fixed_replicas",
        "replicas": replicas,
        "offered_rps": offered,
        "invalid": invalid,
        "http": {
            "successful_rps": rps,
            "error_fraction": errors,
            "latency": {"p95_s": p95},
        },
    }


def test_a_rate_the_service_met_in_full_is_not_reported_as_a_ceiling():
    """The bug this pins: one replica was offered 115 rps and eight replicas
    231 rps, both served in full, and the ratio was published as 25% scaling
    efficiency at eight replicas."""
    results = [
        _point(1, 115.3, 114.7),
        _point(8, 115.3, 114.7),
        _point(8, 230.7, 229.3),
    ]
    at_one = runner.bracketed_capacity(results, kind="fixed_replicas", replicas=1, slo_p95_s=2.0)
    assert at_one["rps"] == 114.7
    assert not at_one["bracketed"]

    headline = runner.capacity_headline(results, 2.0)
    assert headline["replica scaling efficiency at 8"]["value"] is None
    assert "not determined" in headline["replica scaling efficiency at 8"]["evidence"]
    assert "lower bound" in headline["sustainable single-replica capacity (C1)"]["evidence"]


def test_an_efficiency_is_reported_once_both_counts_have_a_measured_ceiling():
    results = [
        _point(1, 115.3, 114.7),
        _point(1, 230.7, 3.0, p95=600.0, errors=0.98),
        _point(8, 922.8, 800.0),
        _point(8, 1845.6, 200.0, p95=90.0, errors=0.4),
    ]
    headline = runner.capacity_headline(results, 2.0)
    entry = headline["replica scaling efficiency at 8"]
    assert entry["value"] == round(800.0 / (8 * 114.7), 3)
    assert "lower bound" not in headline["sustainable single-replica capacity (C1)"]["evidence"]


def test_an_invalid_higher_rung_brackets_nothing():
    """A measurement that described the client cannot establish where the
    service stopped, so the rate below it stays a lower bound."""
    results = [
        _point(1, 115.3, 114.7),
        _point(1, 230.7, 3.0, p95=600.0, errors=0.98, invalid=True),
    ]
    at_one = runner.bracketed_capacity(results, kind="fixed_replicas", replicas=1, slo_p95_s=2.0)
    assert not at_one["bracketed"]
    # And an invalid point is never itself the reported capacity.
    assert at_one["rps"] == 114.7


def test_an_invalid_measurement_cannot_be_the_capacity_it_reports():
    results = [
        _point(1, 115.3, 114.7),
        _point(1, 230.7, 229.0, invalid=True),
    ]
    assert runner.sustainable_capacity(results, 2.0) == 114.7


# -- stage timings across clocks of different resolution ---------------------


def _keda_inputs(*, pod_created, step=2.0):
    """One scale-out, with the scaler series sampled at `step` seconds."""
    metrics = [
        {"metric": "keda_scaler_metrics_value", "labels": "", "t": 1000.0 + i * step, "value": v}
        for i, v in enumerate([10.0, 90.0, 90.0, 90.0, 90.0, 90.0])
    ]
    watcher = [
        {"t": 1000.0, "deployments": {"skao-tap-tap-executor": {"spec_replicas": 1}}},
        {"t": 1010.0, "deployments": {"skao-tap-tap-executor": {"spec_replicas": 3}}},
    ]
    pods = [
        {
            "pod": "exec-new",
            "component": "tap-executor",
            "created": pod_created,
            "scheduled": pod_created + 1,
            "container_started": pod_created + 2,
            "ready": pod_created + 4,
        }
    ]
    return metrics, watcher, pods


def test_a_stage_shorter_than_the_clocks_can_resolve_is_zero_not_negative():
    """The bug this pins: a Pod created 1.5 s before the HPA change Prometheus
    reported it from — the two are stamped at different resolutions — was
    published as a pod_creation of -1.5 seconds."""
    metrics, watcher, pods = _keda_inputs(pod_created=1008.5)
    result = keda.timings(
        t0=1000.0,
        metrics_rows=metrics,
        watcher_samples=watcher,
        pod_timings=pods,
        samples=[],
        deployment="skao-tap-tap-executor",
        threshold=60.0,
        slo_p95_s=2.0,
    )
    assert result["latencies_s"]["pod_creation"] == 0.0
    assert any("reported as 0 rather than negative" in n for n in result["notes"])


def test_a_stage_ordered_backwards_beyond_that_is_not_reported_at_all():
    metrics, watcher, pods = _keda_inputs(pod_created=1005.0)
    result = keda.timings(
        t0=1000.0,
        metrics_rows=metrics,
        watcher_samples=watcher,
        pod_timings=pods,
        samples=[],
        deployment="skao-tap-tap-executor",
        threshold=60.0,
        slo_p95_s=2.0,
    )
    assert result["latencies_s"]["pod_creation"] is None
    assert any("out of order" in n for n in result["notes"])


def test_the_resolution_is_measured_from_the_series_not_assumed():
    rows = [{"metric": "m", "labels": "", "t": t, "value": 0.0} for t in (0.0, 5.0, 10.0, 15.0)]
    assert keda._series_step(rows) == 5.0
    # A single point cannot establish a step; the fallback must not be zero,
    # which would make every tolerance vanish.
    assert keda._series_step(rows[:1]) == 1.0


def test_a_scale_out_whose_pods_are_gone_is_timed_from_the_watcher():
    """The gap this closes: a scenario that scaled up and back down had the
    pods that served the scale-out deleted before the run ended, so the
    end-of-run Pod query returned none and T3 to T6 were simply absent."""
    metrics, watcher, _ = _keda_inputs(pod_created=1012.0)
    for sample in watcher:
        sample["pods"] = [
            {"name": "skao-tap-tap-executor-abc", "created": 1012.0, "ready": sample["t"] >= 1010.0}
        ]
    result = keda.timings(
        t0=1000.0,
        metrics_rows=metrics,
        watcher_samples=watcher,
        pod_timings=[],  # the deployment scaled back down; nothing survives
        samples=[],
        deployment="skao-tap-tap-executor",
        threshold=60.0,
        slo_p95_s=2.0,
    )
    assert result["stamps"]["T3"] == 1012.0
    assert result["stamps"]["T6"] == 1010.0
    assert any("gone by the end of the run" in n for n in result["notes"])
    # Stages the watcher does not record stay absent rather than being filled.
    assert result["latencies_s"]["scheduling"] is None
    assert result["latencies_s"]["container_start"] is None


def test_a_flapping_scenario_says_its_stages_may_straddle_cycles():
    metrics, watcher, pods = _keda_inputs(pod_created=1012.0)
    watcher += [
        {"t": 1020.0, "deployments": {"skao-tap-tap-executor": {"spec_replicas": 1}}},
        {"t": 1030.0, "deployments": {"skao-tap-tap-executor": {"spec_replicas": 3}}},
    ]
    result = keda.timings(
        t0=1000.0,
        metrics_rows=metrics,
        watcher_samples=watcher,
        pod_timings=pods,
        samples=[],
        deployment="skao-tap-tap-executor",
        threshold=60.0,
        slo_p95_s=2.0,
    )
    assert any("moved 3 times" in n for n in result["notes"])


def test_a_scale_out_delay_is_named_rather_than_left_unknown():
    """The dead rule this revives: KEDA_SCALE_LAG was written, listed in
    CLASSES, and never reachable — nothing passed `keda` to classify(). A
    scenario whose 566-second p95 was entirely the scaling delay, on a fleet
    where nothing was saturated, came out UNKNOWN."""
    rows = _rows(("tap_api_cpu_cores", [0.1] * 50), ("api_replicas_ready", [3.0] * 50))
    quiet = {
        "metrics_rows": rows,
        "summary": {"window_seconds": 900.0, "requests": 13000, "error_fraction": 0.001},
        "pg_summary": {"cache_hit_ratio": 1.0},
        "recorder_cpu_peak": 0.07,
        "limits": LIMITS,
    }
    assert bottleneck.primary(bottleneck.classify(**quiet)) != "KEDA_SCALE_LAG"

    lagging = bottleneck.classify(**quiet, keda={"latencies_s": {"total_scale_out": 352.0}})
    assert bottleneck.primary(lagging) == "KEDA_SCALE_LAG"
    evidence = next(v for v in lagging if v.classification == "KEDA_SCALE_LAG").evidence
    assert evidence["total_scale_out_s"] == 352.0
