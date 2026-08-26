"""Tests for what a (workers, replicas) point means.

The worker sweep deploys worker counts the values file does not state, and
every ceiling in the suite — the CPU limit a pod is graded against, the pool
a fleet is allowed, the capacity a grid point publishes — has to follow the
count actually deployed. Each of these would otherwise fail the same way:
a confident number graded against the wrong fleet shape.
"""

import pathlib
import sys

SUITE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE))

from egernia_bench.analyze import bottleneck  # noqa: E402
from egernia_bench.orchestrate import runner  # noqa: E402


def _sweep_point(workers, replicas, concurrency, rps, p95=0.05, saturation_count=0):
    point = {
        "key": f"wsweep-w{workers}-n{replicas}-D1-c{concurrency}-r0",
        "kind": "worker_sweep",
        "workers": workers,
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


# -- the grid's points are separated by both axes ----------------------------


def test_two_worker_counts_at_the_same_replica_count_are_separate_ceilings():
    """The grid holds w=1,n=2 and w=2,n=2 in the same result list. Pooled by
    replicas alone, the two-worker plateau would bracket the one-worker
    ladder and its capacity would be published for both shapes."""
    results = [
        _sweep_point(1, 2, 8, 190.0),
        _sweep_point(1, 2, 16, 195.0, saturation_count=2),
        _sweep_point(2, 2, 16, 360.0),
        _sweep_point(2, 2, 32, 370.0, saturation_count=2),
    ]
    one = runner.bracketed_capacity(
        results, kind="worker_sweep", replicas=2, workers=1, slo_p95_s=2.0
    )
    two = runner.bracketed_capacity(
        results, kind="worker_sweep", replicas=2, workers=2, slo_p95_s=2.0
    )
    assert one["rps"] == 195.0 and one["bracketed"]
    assert two["rps"] == 370.0 and two["bracketed"]


def test_a_workers_filter_of_none_means_the_family_does_not_vary_it():
    """The replica families predate the workers field; filtering their rows
    on it would empty every ladder they ever measured."""
    legacy = {
        "key": "rsweep-n1-D1-c4-r0",
        "kind": "replica_sweep",
        "replicas": 1,
        "concurrency": 4,
        "invalid": False,
        "saturation": {"count": 2},
        "http": {"successful_rps": 99.0, "error_fraction": 0.0, "latency": {"p95_s": 0.05}},
    }
    found = runner.bracketed_capacity([legacy], kind="replica_sweep", replicas=1, slo_p95_s=2.0)
    assert found and found["rps"] == 99.0


# -- the connection arithmetic is stated with every capacity -----------------


def test_worker_capacities_state_the_pool_ceiling_each_shape_implies():
    results = [
        _sweep_point(1, 1, 4, 99.0, saturation_count=2),
        _sweep_point(4, 8, 64, 900.0, saturation_count=2),
    ]
    rows = runner.worker_capacities(results, 2.0, pool_max=8, max_connections=200, executor_pool=8)
    by_point = {(r["workers"], r["replicas"]): r for r in rows}
    small = by_point[(1, 1)]
    assert small["connection_ceiling"] == 8
    assert not small["exceeds_max_connections"]
    big = by_point[(4, 8)]
    # 8 pods x 4 workers x 8 connections: more than a 200-connection server
    # can honour once 3 superuser slots and the executor's 8 are held back.
    assert big["connection_ceiling"] == 256
    assert big["exceeds_max_connections"]
    assert big["worker_processes"] == 32


def test_a_shape_just_inside_the_usable_connections_is_not_flagged():
    """usable = max_connections - 3 superuser slots - the executor's pool;
    the same accounting the chart's HPA guard applies. 2x8x8=128 fits inside
    200-3-8=189 and must not be flagged by a comparison against a limit that
    forgot the holdbacks — or one that double-counted them."""
    results = [_sweep_point(2, 8, 32, 700.0, saturation_count=2)]
    rows = runner.worker_capacities(results, 2.0, pool_max=8, max_connections=200, executor_pool=8)
    (row,) = rows
    assert row["connection_ceiling"] == 128
    assert not row["exceeds_max_connections"]
    # and a server small enough that it no longer fits
    (tight,) = runner.worker_capacities(
        results, 2.0, pool_max=8, max_connections=130, executor_pool=8
    )
    assert tight["exceeds_max_connections"]


def test_worker_capacities_ignore_the_other_families():
    """A replica-sweep row with no workers field is not a grid point."""
    legacy = {
        "key": "rsweep-n1-D1-c4-r0",
        "kind": "replica_sweep",
        "replicas": 1,
        "concurrency": 4,
        "invalid": False,
        "http": {"successful_rps": 99.0, "error_fraction": 0.0, "latency": {"p95_s": 0.05}},
    }
    assert runner.worker_capacities([legacy], 2.0, pool_max=8, max_connections=200) == []


def test_the_arithmetic_is_read_from_the_chart_not_restated():
    values = {
        "config": {"dbPoolMax": 6},
        "postgresql": {"tuning": {"max_connections": "150"}},
        "tapExecutor": {"replicas": 2},
    }
    arithmetic = runner.connection_arithmetic(values)
    assert arithmetic == {"pool_max": 6, "max_connections": 150, "executor_pool": 12}


# -- the deployed worker count, not the values file's ------------------------


def test_limits_follow_the_deployed_worker_count():
    """A two-worker pod graded against the file's one-worker ceiling reads as
    past its limit at half load; a four-worker pod cannot exceed its 2-core
    cgroup however many processes it runs."""
    base = runner.limits_with_workers(
        {"tap_api_workers": 1, "tap_api_cpu_limit_cores": 1.0, "tap_api_pod_cpu_limit_cores": 2.0},
        None,
    )
    assert base["tap_api_cpu_limit_cores"] == 1.0  # untouched
    two = runner.limits_with_workers(base, 2)
    assert two["tap_api_workers"] == 2
    assert two["tap_api_cpu_limit_cores"] == 2.0
    four = runner.limits_with_workers(base, 4)
    assert four["tap_api_cpu_limit_cores"] == 2.0  # the cgroup, not the count


def test_the_limit_probe_is_graded_against_the_limits_it_deployed():
    """The probe raises the pod's CPU and memory limits past the values
    file's; graded against the file's, its pod would read as impossibly past
    its ceiling and every verdict would be about the wrong pod."""
    base = {
        "tap_api_workers": 1,
        "tap_api_cpu_limit_cores": 1.0,
        "tap_api_pod_cpu_limit_cores": 2.0,
        "tap_api_memory_limit_bytes": 1 << 30,
    }
    probe = runner.limits_with_workers(base, 4, pod_cpu=4.0, pod_memory_bytes=2 << 30)
    assert probe["tap_api_cpu_limit_cores"] == 4.0
    assert probe["tap_api_pod_cpu_limit_cores"] == 4.0
    assert probe["tap_api_memory_limit_bytes"] == 2 << 30
    # and the grid's own w=4 point stays capped by the file's 2-core pod
    grid = runner.limits_with_workers(base, 4)
    assert grid["tap_api_cpu_limit_cores"] == 2.0


def test_the_limit_probe_is_not_a_grid_point():
    """Same (workers, replicas) as a grid point, different pod: merged into
    worker_capacities it would overwrite the grid's w=4 capacity with one
    measured against twice the CPU."""
    probe_row = _sweep_point(4, 1, 8, 350.0, saturation_count=2)
    probe_row["kind"] = "worker_limit_probe"
    assert runner.worker_capacities([probe_row], 2.0, pool_max=8, max_connections=200) == []


def test_a_multi_worker_pods_pool_is_per_process_times_workers():
    """The fleet pool ceiling is pods x workers x dbPoolMax: 12 connections
    against one two-worker pod is 12 of 16, not a full single pool."""
    limits = runner.limits_with_workers(
        {
            "tap_api_workers": 1,
            "tap_api_cpu_limit_cores": 1.0,
            "tap_api_pod_cpu_limit_cores": 2.0,
            "postgres_cpu_limit_cores": 4.0,
            "db_pool_max_total": 8,
            "tap_api_memory_limit_bytes": 1 << 30,
        },
        2,
    )
    rows = []
    for metric, values in (
        ("tap_db_connections_in_use", [12.0] * 50),
        ("api_replicas_ready", [1.0] * 50),
    ):
        rows += [
            {"metric": metric, "labels": "", "t": float(i), "value": v}
            for i, v in enumerate(values)
        ]
    verdicts = bottleneck.classify(
        metrics_rows=rows,
        summary={"window_seconds": 60.0, "requests": 1000, "error_fraction": 0.0},
        pg_summary={"cache_hit_ratio": 1.0},
        recorder_cpu_peak=0.05,
        limits=limits,
    )
    assert "CONNECTION_POOL_BOUND" not in {v.classification for v in verdicts}


# -- configuration ------------------------------------------------------------


def test_the_worker_sweep_plan_is_in_the_config():
    plan = runner.load_config()["scenarios"]["worker_sweep"]
    assert plan["workers"] == [1, 2, 4]
    assert plan["replicas"] == [1, 2, 4, 8]
    assert plan["repetitions"] >= 2


# -- memory is judged per pod, not per fleet sum ------------------------------


def _mem_rows(*series):
    rows = []
    for metric, values in series:
        rows += [
            {"metric": metric, "labels": "", "t": float(i), "value": v}
            for i, v in enumerate(values)
        ]
    return rows


def _mem_verdicts(rows):
    return {
        v.classification: v
        for v in bottleneck.classify(
            metrics_rows=rows,
            summary={"window_seconds": 60.0, "requests": 1000, "error_fraction": 0.0},
            pg_summary={"cache_hit_ratio": 1.0},
            recorder_cpu_peak=0.05,
            limits={
                "tap_api_cpu_limit_cores": 2.0,
                "postgres_cpu_limit_cores": 4.0,
                "db_pool_max_total": 8,
                "tap_api_workers": 1,
                "tap_api_memory_limit_bytes": 1 << 30,
            },
        )
    }


def test_a_rollouts_terminating_pods_are_not_a_memory_verdict():
    """The bug this pins: right after a worker-count upgrade the fleet-summed
    working set still counts the previous configuration's terminating pods —
    nine pods against a one-pod allowance read as 3.3 GiB over a 1 GiB limit,
    and six transition rungs were called MEMORY_BOUND at 130 MiB a worker."""
    rows = _mem_rows(
        ("tap_api_memory_bytes", [3.3e9] * 50),  # sum: 8 dying pods + 1 new
        ("tap_api_memory_max_bytes", [560e6] * 50),  # busiest single pod
        ("api_replicas_ready", [1.0] * 50),
    )
    assert "MEMORY_BOUND" not in _mem_verdicts(rows)


def test_one_pod_near_its_limit_is_memory_bound_whatever_the_fleet_average():
    """The same mistake in the other direction: eight pods averaging 500 MiB
    hide one at 990 MiB from a fleet-sum comparison."""
    rows = _mem_rows(
        ("tap_api_memory_bytes", [4.5e9] * 50),  # comfortable against 8 GiB
        ("tap_api_memory_max_bytes", [990e6] * 50),
        ("api_replicas_ready", [8.0] * 50),
    )
    verdict = _mem_verdicts(rows)["MEMORY_BOUND"]
    assert verdict.evidence["api_peak_is_per_pod"]
    assert verdict.evidence["api_limit_bytes"] == 1 << 30


def test_artefacts_without_the_per_pod_series_keep_the_fleet_judgement():
    """Runs measured before the per-pod series existed can still be
    reclassified; they get the old fleet arithmetic, not a crash or silence."""
    rows = _mem_rows(
        ("tap_api_memory_bytes", [7.8e9] * 50),
        ("api_replicas_ready", [8.0] * 50),
    )
    verdict = _mem_verdicts(rows)["MEMORY_BOUND"]
    assert not verdict.evidence["api_peak_is_per_pod"]


# -- restarts are judged per window, not per pod lifetime ---------------------


def test_a_restart_before_the_window_does_not_taint_the_measurement():
    """restartCount is the pod's whole history. Judged cumulatively, one
    crash taints every measurement that pod appears in afterwards — a
    12-point sweep would publish eleven invalid results for one restart
    during its first."""
    from egernia_bench.collect import guards

    pods = [{"pod": "tap-api-abc", "component": "tap-api", "restarts": 2}]
    guard = guards.Guards(min_free_disk_gb=0)

    def verdict(baseline):
        results = guard.evaluate(pod_timings=pods, restarts_before=baseline)
        return {r.name: r for r in results}["no_unexpected_restarts"]

    # the 2 restarts predate this window: clean
    assert verdict({"tap-api-abc": 2}).ok
    # one of them happened inside the window: tainted
    assert not verdict({"tap-api-abc": 1}).ok
    # a pod born during the window carrying restarts: the window's own
    assert not verdict({}).ok
    # no baseline taken: the old cumulative judgement, unchanged
    assert not verdict(None).ok
