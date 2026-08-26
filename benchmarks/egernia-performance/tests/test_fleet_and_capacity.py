"""Tests for what a multi-replica number means.

Every API series the classifier reads is summed over the pods, and every rate
the report divides is a rate somebody chose to offer. Both invite the same
mistake — comparing a fleet's total against one pod's ceiling, dividing two
rates neither of which is a limit — and both produce a confident wrong answer
rather than a missing one. These pin the corrections.
"""

import pathlib
import sys

import pytest

SUITE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE))

from egernia_bench.analyze import bottleneck, keda  # noqa: E402
from egernia_bench.collect import guards  # noqa: E402
from egernia_bench.orchestrate import runner  # noqa: E402

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


def test_c1_is_the_deployed_shape_not_the_worker_sweeps_off_default_points():
    """C1 anchors the autoscaling multiples, which run at the values-file
    defaults — so a single replica on a raised CPU limit (the limit probe)
    or with extra workers must not be read as the deployed capacity."""
    deployed = _point(1, 100.0, 99.1)
    probe = _point(1, 340.0, 338.1)
    probe["pod_cpu_limit"] = 4.0
    probe["kind"] = "worker_limit_probe"
    two_workers = _point(1, 190.0, 189.0)
    two_workers["workers"] = 2
    assert runner.sustainable_capacity([deployed, probe, two_workers], 2.0) == 99.1


# -- stage timings across clocks of different resolution ---------------------


def _keda_inputs(*, pod_created, step=2.0):
    """One scale-out, with the scaler series sampled at `step` seconds."""
    metrics = [
        {"metric": "keda_scaler_metrics_value", "labels": "", "t": 1000.0 + i * step, "value": v}
        for i, v in enumerate([10.0, 90.0, 90.0, 90.0, 90.0, 90.0])
    ]
    watcher = [
        {"t": 1000.0, "deployments": {"egernia-tap-executor": {"spec_replicas": 1}}},
        {"t": 1010.0, "deployments": {"egernia-tap-executor": {"spec_replicas": 3}}},
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
        deployment="egernia-tap-executor",
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
        deployment="egernia-tap-executor",
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
            {"name": "egernia-tap-executor-abc", "created": 1012.0, "ready": sample["t"] >= 1010.0}
        ]
    result = keda.timings(
        t0=1000.0,
        metrics_rows=metrics,
        watcher_samples=watcher,
        pod_timings=[],  # the deployment scaled back down; nothing survives
        samples=[],
        deployment="egernia-tap-executor",
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
        {"t": 1020.0, "deployments": {"egernia-tap-executor": {"spec_replicas": 1}}},
        {"t": 1030.0, "deployments": {"egernia-tap-executor": {"spec_replicas": 3}}},
    ]
    result = keda.timings(
        t0=1000.0,
        metrics_rows=metrics,
        watcher_samples=watcher,
        pod_timings=pods,
        samples=[],
        deployment="egernia-tap-executor",
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
    assert bottleneck.classify(**quiet)[0].classification != "KEDA_SCALE_LAG"

    lagging = bottleneck.classify(**quiet, keda={"latencies_s": {"total_scale_out": 352.0}})
    assert lagging[0].classification == "KEDA_SCALE_LAG"
    evidence = next(v for v in lagging if v.classification == "KEDA_SCALE_LAG").evidence
    assert evidence["total_scale_out_s"] == 352.0


def test_a_pinned_executor_is_reported_rather_than_read_as_headroom():
    """The gap this closes: each executor pod sat at 0.96 of a core against a
    two-core cgroup with no throttling, and no rule looked at it — so an
    autoscaling run whose executors were saturated was described as having
    nothing busy."""
    rows = _rows(
        ("tap_executor_cpu_cores", [2.89] * 50),
        ("executor_replicas_ready", [3.0] * 50),
    )
    classes = {v.classification for v in _classify(rows)}
    assert "EXECUTOR_CPU_BOUND" in classes

    # Three pods at a third of a core each is not the same measurement.
    idle = _rows(
        ("tap_executor_cpu_cores", [1.0] * 50),
        ("executor_replicas_ready", [3.0] * 50),
    )
    assert "EXECUTOR_CPU_BOUND" not in {v.classification for v in _classify(idle)}


def test_the_executor_ceiling_is_one_core_a_pod_not_its_cgroup():
    """One executor runs one query at a time, so the cgroup's two cores are not
    the ceiling — comparing against them reports a pinned pod as half idle."""
    from egernia_bench.orchestrate import runner as runner_mod

    resolved = runner_mod.limits(
        {
            "chart_values": {
                "tapExecutor": {"resources": {"limits": {"cpu": 2}}},
                "tapApi": {"workers": 1, "resources": {"limits": {"cpu": 2}}},
            }
        }
    )
    assert resolved["tap_executor_cpu_limit_cores"] == 1.0
    assert resolved["tap_executor_pod_cpu_limit_cores"] == 2.0


# ---------------------------------------------------------------------------
# The closed-loop replica sweep (package 14)
# ---------------------------------------------------------------------------


def _sweep_point(replicas, concurrency, rps, *, p95=0.06, saturation_count=0):
    point = {
        "key": f"rsweep-n{replicas}-c{concurrency}-r0",
        "kind": "replica_sweep",
        "replicas": replicas,
        "concurrency": concurrency,
        "invalid": False,
        "http": {
            "successful_rps": rps,
            "error_fraction": 0.0,
            "latency": {"p95_s": p95},
        },
    }
    if saturation_count:
        point["saturation"] = {"count": saturation_count}
    return point


def test_a_saturated_sweep_is_a_bracket_without_any_slo_failure():
    """A CPU-bound service under a closed loop degrades in latency, not
    errors: at its throughput ceiling it can sit far inside a 2 s SLO, so
    the plateau — two saturation signals — is the ceiling's evidence."""
    results = [
        _sweep_point(1, 4, 180.0),
        _sweep_point(1, 8, 205.0),
        _sweep_point(1, 16, 207.0, p95=0.12, saturation_count=2),
    ]
    at_one = runner.bracketed_capacity(results, kind="replica_sweep", replicas=1, slo_p95_s=2.0)
    assert at_one["bracketed"]
    assert at_one["saturated"]
    assert at_one["rps"] == 207.0


def test_an_unsaturated_sweep_stays_open_ended():
    results = [_sweep_point(2, 4, 300.0), _sweep_point(2, 8, 390.0)]
    at_two = runner.bracketed_capacity(results, kind="replica_sweep", replicas=2, slo_p95_s=2.0)
    assert not at_two["bracketed"]


def test_the_efficiency_headline_populates_from_saturated_sweeps():
    results = [
        _sweep_point(1, 8, 205.0),
        _sweep_point(1, 16, 207.0, saturation_count=2),
        _sweep_point(8, 64, 1245.0),
        _sweep_point(8, 128, 1260.0, saturation_count=2),
    ]
    headline = runner.capacity_headline(results, 2.0)
    entry = headline["replica scaling efficiency at 8"]
    assert entry["value"] == round(1260.0 / (8 * 207.0), 3)
    assert "saturated closed-loop sweep" in entry["evidence"]


def test_replica_capacities_prefer_the_bracketed_kind():
    """An open-ended open-loop point must not shadow a sweep that measured
    the ceiling for the same count."""
    results = [
        _point(1, 500.0, 499.0),  # fixed_replicas: served in full, no bracket
        _sweep_point(1, 16, 207.0, saturation_count=2),
    ]
    rows = runner.replica_capacities(results, 2.0)
    (row,) = rows
    assert row["kind"] == "replica_sweep"
    assert row["bracketed"]
    assert row["rps"] == 207.0


def test_a_saturated_low_rung_under_a_climbing_top_rung_is_no_bracket():
    """The evidence for a ceiling has to come from where the ladder stopped.
    Two signals at c8 with c16 still gaining throughput is a ladder that
    never found a plateau — an any()-over-the-ladder test would have called
    it bracketed and published the ratio as a scaling efficiency."""
    results = [
        _sweep_point(1, 4, 180.0),
        _sweep_point(1, 8, 205.0, saturation_count=2),
        _sweep_point(1, 16, 260.0),
    ]
    at_one = runner.bracketed_capacity(results, kind="replica_sweep", replicas=1, slo_p95_s=2.0)
    assert not at_one["saturated"]
    assert not at_one["bracketed"]


def test_the_top_rung_saturates_on_whichever_repetition_carries_the_signals():
    """A rung is `repetitions` measurements and only the median one gets the
    saturation block, so the bracket cannot depend on which of them is seen
    first."""
    results = [
        _sweep_point(1, 8, 205.0),
        _sweep_point(1, 16, 207.0),
        _sweep_point(1, 16, 206.0, saturation_count=2),
    ]
    at_one = runner.bracketed_capacity(results, kind="replica_sweep", replicas=1, slo_p95_s=2.0)
    assert at_one["saturated"]
    assert at_one["bracketed"]


def test_the_bracket_uses_the_configured_signal_count_not_a_hardcoded_two():
    """Raise concurrency_sweep.saturation_signals_required and the ladder
    climbs past two agreeing signals — an analyzer still stopping at two
    would report a bracketed ceiling for a sweep that never saturated."""
    results = [_sweep_point(1, 8, 205.0), _sweep_point(1, 16, 207.0, saturation_count=2)]
    kwargs = {"kind": "replica_sweep", "replicas": 1, "slo_p95_s": 2.0}
    assert runner.bracketed_capacity(results, **kwargs, signals_required=2)["bracketed"]
    assert not runner.bracketed_capacity(results, **kwargs, signals_required=3)["bracketed"]
    # and the configured value is what the production paths read
    assert runner.saturation_signals_required(runner.load_config()) == 2
    assert runner.saturation_signals_required({"scenarios": {"concurrency_sweep": {}}}) == 2
    assert (
        runner.saturation_signals_required(
            {"scenarios": {"concurrency_sweep": {"saturation_signals_required": 3}}}
        )
        == 3
    )


# ---------------------------------------------------------------------------
# The three recorded misreadings (package 15)
# ---------------------------------------------------------------------------


def test_pool_confidence_grades_against_the_timeout():
    """The bug this pins: min(1.0, wait) made any wait over one second full
    confidence, so the class outranked everything wherever the pool waited
    at all. A 0.5 s wait against a 5 s timeout is a tenth of a case."""
    rows = _rows(
        ("tap_db_connections_in_use", [2.0] * 50),
        ("tap_pool_wait_p95", [0.5] * 50),
        ("api_replicas_ready", [1.0] * 50),
    )
    verdicts = {v.classification: v for v in _classify(rows)}
    pool = verdicts["CONNECTION_POOL_BOUND"]
    assert pool.confidence == 0.1
    assert pool.evidence["pool_timeout_s"] == 5.0


def test_a_wait_at_the_timeout_is_still_full_confidence():
    rows = _rows(
        ("tap_db_connections_in_use", [8.0] * 50),
        ("tap_pool_wait_p95", [5.0] * 50),
        ("api_replicas_ready", [1.0] * 50),
    )
    verdicts = {v.classification: v for v in _classify(rows)}
    assert verdicts["CONNECTION_POOL_BOUND"].confidence == 1.0


def test_a_ramping_fleet_is_judged_against_the_pods_ready_at_each_sample():
    """The bug this pins: the executor ceiling was the *peak* ready count
    times one core across the whole window, so a fleet pinned at 2, then 4,
    then 8 cores — pinned the entire time — read UNKNOWN against an 8-core
    ceiling it only reached at the end."""
    cpu = [1.9] * 40 + [7.9] * 10
    ready = [2.0] * 40 + [8.0] * 10
    rows = _rows(
        ("tap_executor_cpu_cores", cpu),
        ("executor_replicas_ready", ready),
    )
    limits = {**LIMITS, "tap_executor_cpu_limit_cores": 1.0}
    verdicts = {
        v.classification: v
        for v in bottleneck.classify(
            metrics_rows=rows,
            summary={"window_seconds": 60.0, "requests": 100, "error_fraction": 0.0},
            pg_summary={"cache_hit_ratio": 1.0},
            recorder_cpu_peak=0.05,
            limits=limits,
        )
    }
    pinned = verdicts["EXECUTOR_CPU_BOUND"]
    assert pinned.evidence["fraction_of_window_above_90pct_limit"] == 1.0

    # and the old reading, for contrast: against the peak-sized ceiling only
    # the final third is hot, which is below the rule's threshold
    peak_limit = 8.0
    old_hot = sum(1 for v in cpu if v > 0.9 * peak_limit) / len(cpu)
    assert old_hot < 0.25


def test_a_fleet_scaling_mid_window_does_not_overstate_the_ceiling_mid_ramp():
    """Mid-ramp, usage at the then-fleet's ceiling counts as hot even though
    the window's eventual peak fleet is larger."""
    at = bottleneck.aligned_fleet(
        _rows(("executor_replicas_ready", [1.0] * 10 + [4.0] * 10)),
        __import__("numpy").asarray([0.0, 5.0, 12.0, 19.0]),
        "executor_replicas_ready",
    )
    assert list(at) == [1.0, 1.0, 4.0, 4.0]


def test_a_ramping_fleet_still_reads_as_serialization_bound():
    """The gate for SERIALIZATION_BOUND is 60% of the API's ceiling, which
    makes it the most sensitive of the three to an overstated one: against the
    peak fleet a run that spent its window formatting bytes reads as only a
    fifth busy, misses the 0.25 threshold, and is filed as UNKNOWN."""
    cpu = [1.5] * 40 + [6.0] * 10
    ready = [2.0] * 40 + [8.0] * 10
    rows = _rows(
        ("tap_api_cpu_cores", cpu),
        ("api_replicas_ready", ready),
        ("postgres_cpu_cores", [0.5] * 50),
    )
    verdicts = {
        v.classification: v
        for v in bottleneck.classify(
            metrics_rows=rows,
            summary={
                "window_seconds": 60.0,
                "requests": 1000,
                "error_fraction": 0.0,
                "response_bytes_total": 500_000_000.0,
                "response_throughput_bytes_per_s": 8.3e6,
            },
            pg_summary={"cache_hit_ratio": 1.0},
            recorder_cpu_peak=0.05,
            limits=LIMITS,
        )
    }
    # busy against the fleet that was ready at each sample, the whole window
    assert verdicts["SERIALIZATION_BOUND"].evidence["api_busy_fraction"] == 1.0
    # neither pod was near its own ceiling, so this is not the CPU class
    assert "TAP_CPU_BOUND" not in verdicts

    # and the old reading, for contrast: against the peak-sized ceiling only
    # the final fifth is busy, which is below the rule's threshold
    peak_limit = LIMITS["tap_api_cpu_limit_cores"] * 8.0
    old_busy = sum(1 for v in cpu if v > 0.60 * peak_limit) / len(cpu)
    assert old_busy < 0.25


def test_an_untimed_metrics_row_is_refused_rather_than_silently_partial():
    """The timed path is only fallen back on when it is *empty*, so a metric
    whose rows were partly timestamped would judge the window on whichever
    subset carried a `t` and report it as the whole. Every row from a
    measurement is timestamped — Prometheus.collect() writes `t` on all of
    them and the metrics Parquet has it as a column — so a row without one is
    a caller bug, and it says so instead of computing a plausible number."""
    rows = _rows(("tap_api_cpu_cores", [0.5] * 10), ("api_replicas_ready", [1.0] * 10))
    del rows[3]["t"]
    with pytest.raises(ValueError, match="tap_api_cpu_cores: 1 of 10 rows carry no 't'"):
        _classify(rows)

    # fully timestamped, the same rows classify normally
    assert _classify(_rows(("tap_api_cpu_cores", [0.5] * 10), ("api_replicas_ready", [1.0] * 10)))


def test_a_run_measured_before_the_rename_still_names_its_deployment():
    """The bug this pins: the Deployment name carries the Helm release, so
    renaming the release (skao-tap to egernia) would have made every stored
    run's fleet unreadable — `reclassify` would look for `egernia-tap-executor`
    in samples that only ever recorded `skao-tap-tap-executor` and re-derive an
    empty fleet, which reads as "nothing scaled" rather than as "not found"."""
    legacy = [
        {"t": 1000.0, "deployments": {"skao-tap-tap-executor": {"spec_replicas": 1}}},
        {"t": 1010.0, "deployments": {"skao-tap-tap-executor": {"spec_replicas": 3}}},
    ]
    current = [{"t": 1000.0, "deployments": {"egernia-tap-executor": {"spec_replicas": 1}}}]
    assert runner._watched_deployment(legacy, "tap-executor") == "skao-tap-tap-executor"
    assert runner._watched_deployment(current, "tap-executor") == "egernia-tap-executor"
    # Nothing recorded: the current release's name, so a live run is unaffected.
    assert runner._watched_deployment([], "tap-executor") == "egernia-tap-executor"
