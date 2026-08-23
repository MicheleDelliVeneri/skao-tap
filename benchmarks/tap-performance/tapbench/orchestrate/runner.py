"""Orchestration: what actually runs, in what order, and what it records.

One measurement is the unit: reset the database's statement statistics, take a
statistics snapshot, generate load for a fixed window, snapshot again, read the
window back out of Prometheus, judge the validity guards, and only then compute
anything. Each step exists because skipping it makes a number that looks fine
and is not attributable to anything.

Every measurement writes its own Parquet and its own marker, so a matrix that
dies at hour nine resumes at hour nine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import typing

import numpy as np
import yaml

from .. import cluster
from .. import corpus as corpus_mod
from .. import runs as runs_mod
from ..analyze import bottleneck
from ..analyze import keda as keda_analysis
from ..analyze import stats as stats_mod
from ..collect import guards as guards_mod
from ..collect import kube
from ..collect import postgres as pg_mod
from ..collect import prometheus as prom_mod
from ..dataset import generate as dataset_mod
from ..load import runner as load_mod

log = logging.getLogger("tapbench.orchestrate")

SUITE = runs_mod.SUITE
BASE_URL = "http://127.0.0.1:30080"
PROMETHEUS_URL = "http://127.0.0.1:30090"

# Resource limits, needed to say "90% of the limit" rather than "1.8 cores".
# Read from the values file so the two cannot disagree.
LIMITS_FROM_VALUES = {
    "tap_api_cpu_limit_cores": ("tapApi", "resources", "limits", "cpu"),
    "postgres_cpu_limit_cores": ("postgresql", "resources", "limits", "cpu"),
}


def load_config() -> dict:
    return {
        "hardware": yaml.safe_load((SUITE / "config/hardware.yaml").read_text()),
        "datasets": yaml.safe_load((SUITE / "config/datasets.yaml").read_text()),
        "scenarios": yaml.safe_load((SUITE / "config/scenarios.yaml").read_text()),
        "chart_values_text": (SUITE / "config/chart-values.yaml").read_text(),
        "chart_values": yaml.safe_load((SUITE / "config/chart-values.yaml").read_text()),
    }


def _quantity(value: typing.Any, default: float) -> float:
    """Kubernetes CPU quantity to cores."""
    if value is None:
        return default
    text = str(value)
    return float(text[:-1]) / 1000.0 if text.endswith("m") else float(text)


def limits(cfg: dict) -> dict:
    values = cfg["chart_values"]

    def dig(path):
        node = values
        for key in path:
            node = (node or {}).get(key)
        return node

    api_cpu = _quantity(dig(LIMITS_FROM_VALUES["tap_api_cpu_limit_cores"]), 2.0)
    pg_cpu = _quantity(dig(LIMITS_FROM_VALUES["postgres_cpu_limit_cores"]), 4.0)
    pool = int((values.get("config") or {}).get("dbPoolMax") or 8)
    workers = int((values.get("tapApi") or {}).get("workers") or 1)
    # The CPU ceiling that matters for the API is not the pod's limit: ADQL
    # translation is pure-Python and holds the GIL, so one uvicorn worker
    # cannot exceed one core however many the cgroup allows. Comparing against
    # the pod limit reports a pinned process as having headroom, which is
    # exactly the mistake that makes a CPU-bound run look unexplained.
    return {
        "tap_api_cpu_limit_cores": min(api_cpu, float(workers)),
        "tap_api_pod_cpu_limit_cores": api_cpu,
        "tap_api_workers": workers,
        "postgres_cpu_limit_cores": pg_cpu,
        "db_pool_max_total": pool,
        "tap_api_memory_limit_bytes": 1 << 30,
        "postgres_memory_limit_bytes": 6 << 30,
    }


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def setup(cfg: dict, *, rebuild_images: bool = True) -> dict:
    """Cluster, KEDA, monitoring, chart. Returns image digests."""
    problems = cluster.preflight(cfg["hardware"]["enforcement"]["min_free_disk_gb"])
    if problems:
        raise SystemExit("preflight failed:\n  " + "\n  ".join(problems))
    enforcement = cfg["hardware"]["enforcement"]
    cluster.create(enforcement["kind_node_cpus"], enforcement["kind_node_memory"])
    if rebuild_images:
        tag, digests = cluster.build_and_load_images()
    else:
        # Keep measuring whatever the release already runs rather than
        # silently reverting to a mutable tag.
        tag, digests = cluster.deployed_image_tag(), {}
    # Pinned globally, not just for this one install: set_autoscaling() and
    # scale() upgrade the release again later, and an upgrade that forgets the
    # tag redeploys whatever the values file says.
    cluster.use_image_tag(tag)
    cluster.install_keda()
    cluster.install_monitoring()
    cluster.install_chart()
    if tag:
        cluster.verify_running_images(tag)
    prometheus = prom_mod.Prometheus(PROMETHEUS_URL)
    if not prometheus.ready():
        raise SystemExit("Prometheus did not become ready")
    prometheus.close()
    return digests


def ensure_dataset(cfg: dict, names: list[str], out_dir) -> dict:
    """Grow the in-cluster database to each named target."""
    targets = [d for d in cfg["datasets"]["datasets"] if d["name"] in names]
    forward = cluster.port_forward_database()
    try:
        built = dataset_mod.build(cluster.database_dsn(), cfg["datasets"], targets, out_dir)
    finally:
        forward.terminate()
    return {stat.name: stat.to_json() for stat in built}


# ---------------------------------------------------------------------------
# One measurement
# ---------------------------------------------------------------------------


def aggregate_resources(rows: list[dict]) -> dict:
    """Mean, peak and final value per metric, over the measured window."""
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row["metric"], []).append(row["value"])
    out: dict[str, float] = {}
    for metric, values in grouped.items():
        array = np.asarray(values, dtype=float)
        array = array[~np.isnan(array)]
        if not array.size:
            continue
        out[f"{metric}_mean"] = float(array.mean())
        out[f"{metric}_max"] = float(array.max())
        out[f"{metric}_p95"] = float(np.percentile(array, 95))
    return out


def measure(
    run,
    cfg: dict,
    *,
    key: str,
    kind: str,
    dataset: str,
    workload: load_mod.Workload,
    mode: str,
    concurrency: int | None = None,
    steps: list[load_mod.Step] | None = None,
    replicas: int | None = None,
    offered_rps: float | None = None,
    repetition: int = 0,
    warmup_s: float | None = None,
    measure_s: float | None = None,
    request_mode: str = "sync",
) -> dict | None:
    """Run one load window and record everything about it."""
    if run.done(key):
        log.info("%s already measured, skipping", key)
        return json.loads((run.path / "state" / f"{key}.done").read_text()).get("result")

    timing = cfg["scenarios"]["timing"]
    warmup_s = timing["warmup_seconds"] if warmup_s is None else warmup_s
    measure_s = timing["measure_seconds"] if measure_s is None else measure_s

    prometheus = prom_mod.Prometheus(PROMETHEUS_URL)
    forward = cluster.port_forward_database()
    conn = pg_mod.connect(cluster.database_dsn())
    guard = guards_mod.Guards(min_free_disk_gb=cfg["hardware"]["enforcement"]["min_free_disk_gb"])
    try:
        pg_mod.reset_statements(conn)
        before = pg_mod.snapshot(conn)
        activity_samples = [pg_mod.activity(conn)]

        watcher_path = run.kube_dir / f"{key}-state.jsonl"
        started = time.time()
        with kube.StateWatcher(watcher_path):
            if steps is not None:
                recorder, timeline = asyncio.run(
                    load_mod.open_loop(
                        BASE_URL,
                        workload,
                        steps,
                        mode=request_mode,
                    )
                )
            else:
                recorder = asyncio.run(
                    load_mod.closed_loop(
                        BASE_URL,
                        workload,
                        concurrency or 1,
                        warmup_s,
                        measure_s,
                        mode=request_mode,
                    )
                )
                timeline = []
            window_end = time.time()
        activity_samples.append(pg_mod.activity(conn))
        after = pg_mod.snapshot(conn)
        statements = pg_mod.statements(conn)
    finally:
        conn.close()
        forward.terminate()

    # The measured window only: for a closed-loop run that is the part after
    # the warmup, and Prometheus is queried for exactly that span so a cold
    # cache cannot leak into the resource figures.
    measured_from = window_end - (measure_s if steps is None else (window_end - started))
    metrics_rows, coverage = prometheus.collect(measured_from - 2, window_end + 2)
    prometheus.close()

    prometheus_parquet = run.metrics_dir / f"{key}.parquet"
    prom_mod.Prometheus(PROMETHEUS_URL).write(metrics_rows, prometheus_parquet)
    load_mod.write_samples(recorder.samples, run.samples_dir / f"{key}.parquet")
    pg_mod.write_statements_csv(statements, run.postgres_dir / f"{key}-statements.csv")
    run.write_json(f"postgres/{key}-before.json", before)
    run.write_json(f"postgres/{key}-after.json", after)
    delta = pg_mod.delta(before, after)
    run.write_json(f"postgres/{key}-delta.json", delta)

    pod_timings = kube.pod_timings("tap-api") + kube.pod_timings("tap-executor")
    run.write_json(f"kubernetes/{key}-pods.json", pod_timings)
    run.write_json(f"kubernetes/{key}-events.json", kube.events())

    window = window_end - measured_from
    http = stats_mod.summarise(recorder.samples, window)
    by_class = stats_mod.by_query_class(recorder.samples, window)
    resources = aggregate_resources(metrics_rows)
    pg_summary = pg_mod.summarise(delta)

    guard_results = guard.evaluate(
        recorder=recorder,
        prometheus_report=coverage,
        pod_timings=pod_timings,
        metrics_rows=metrics_rows,
    )
    failed = [r for r in guard_results if not r.ok]
    for failure in failed:
        run.invalidate(f"{key}: {failure.name}", {"detail": failure.detail, **failure.measured})

    limits_map = limits(cfg)
    # Per-replica GIL ceiling times the replicas actually serving.
    api_limit = limits_map["tap_api_cpu_limit_cores"] * max(replicas or 1, 1)
    verdicts = bottleneck.classify(
        metrics_rows=metrics_rows,
        summary=http,
        pg_summary=pg_summary,
        recorder_cpu_peak=recorder.generator_cpu_peak,
        limits={**limits_map, "tap_api_cpu_limit_cores": api_limit},
    )

    result = {
        "key": key,
        "kind": kind,
        "dataset": dataset,
        "mode": mode,
        "concurrency": concurrency,
        "replicas": replicas,
        "offered_rps": offered_rps,
        "repetition": repetition,
        "request_mode": request_mode,
        "window_seconds": window,
        "started_at": started,
        "ended_at": window_end,
        "http": http,
        "by_class": by_class,
        "resources": {
            **resources,
            "tap_api_cpu_fraction_of_limit": resources.get("tap_api_cpu_cores_p95", 0.0)
            / api_limit,
            "postgres_cpu_fraction_of_limit": resources.get("postgres_cpu_cores_p95", 0.0)
            / limits_map["postgres_cpu_limit_cores"],
        },
        "postgres": pg_summary,
        "postgres_activity": activity_samples,
        "coordinated_omission": stats_mod.coordinated_omission(recorder.samples),
        "prometheus_coverage": coverage,
        "guards": [{"name": r.name, "ok": r.ok, "detail": r.detail} for r in guard_results],
        "bottleneck": [
            {
                "classification": v.classification,
                "confidence": v.confidence,
                "evidence": v.evidence,
                "explanation": v.explanation,
            }
            for v in verdicts
        ],
        "generator_cpu_peak": recorder.generator_cpu_peak,
        "load_timeline": timeline,
        "invalid": bool(failed),
    }
    run.mark_done(key, {"result": result})
    log.info(
        "%s: %.1f rps, p95 %.0f ms, errors %.2f%%, %s",
        key,
        http.get("rps") or 0.0,
        1000 * ((http.get("latency") or {}).get("p95_s") or 0.0),
        100 * (http.get("error_fraction") or 0.0),
        verdicts[0].classification,
    )
    return result


# ---------------------------------------------------------------------------
# Scenario families
# ---------------------------------------------------------------------------


def concurrency_sweep(
    run, cfg: dict, dataset: str, entries: list, *, quick: bool = False
) -> list[dict]:
    """Climb the concurrency ladder until saturation, then stop.

    The stop rule needs two independent signals to agree. One signal alone is
    routinely noise — a p95 can multiply on a page-cache miss, and throughput
    can plateau for one level and then climb again — and a sweep that stops on
    noise reports a ceiling that is not there.
    """
    sweep = cfg["scenarios"]["concurrency_sweep"]
    timing = cfg["scenarios"]["timing"]
    mix = cfg["scenarios"]["query_mix"]["normal"]
    results: list[dict] = []
    baseline: dict | None = None
    previous: dict | None = None

    levels = sweep["levels"][:3] if quick else sweep["levels"]
    for concurrency in levels:
        saturated_here = False
        repetitions = 1 if quick else timing["repetitions"]
        measured: list[dict] = []
        for repetition in range(repetitions):
            workload = load_mod.Workload(entries, mix, seed=1000 + repetition)
            key = f"conc-{dataset}-c{concurrency}-r{repetition}"
            result = measure(
                run,
                cfg,
                key=key,
                kind="concurrency",
                dataset=dataset,
                workload=workload,
                mode="closed",
                concurrency=concurrency,
                replicas=1,
                repetition=repetition,
                warmup_s=10 if quick else timing["warmup_seconds"],
                measure_s=20 if quick else timing["measure_seconds"],
            )
            if result:
                measured.append(result)
                results.append(result)
            time.sleep(timing["settle_seconds"] if not quick else 2)

        if not measured:
            continue
        # Compare on the median repetition rather than the best: the best of
        # three is a biased estimator, and the point of repeating is to be
        # robust to one bad run, not to find the flattering one.
        current = sorted(measured, key=lambda r: r["http"]["rps"])[len(measured) // 2]
        baseline = baseline or current
        signals = stats_mod.saturation_signals(
            current["http"],
            baseline["http"],
            previous["http"] if previous else None,
            current["resources"],
            sweep["signals"],
        )
        current["saturation"] = signals
        log.info(
            "c=%d tripped %d signals: %s",
            concurrency,
            signals["count"],
            ", ".join(signals["tripped"]) or "none",
        )
        if signals["count"] >= sweep["saturation_signals_required"]:
            saturated_here = True

        if saturated_here and not quick:
            # A saturation point earns the longer, more repeated measurement:
            # this is the number that becomes C1 and that every KEDA scenario
            # is expressed as a multiple of, so it should be the best-measured
            # figure in the suite.
            log.info("saturation at c=%d; re-measuring at length", concurrency)
            for repetition in range(timing["saturation_repetitions"]):
                workload = load_mod.Workload(entries, mix, seed=2000 + repetition)
                key = f"sat-{dataset}-c{concurrency}-r{repetition}"
                result = measure(
                    run,
                    cfg,
                    key=key,
                    kind="saturation",
                    dataset=dataset,
                    workload=workload,
                    mode="closed",
                    concurrency=concurrency,
                    replicas=1,
                    repetition=repetition,
                    measure_s=timing["saturation_measure_seconds"],
                )
                if result:
                    results.append(result)
                time.sleep(timing["settle_seconds"])
            break
        previous = current
    return results


def sustainable_capacity(results: list[dict], slo_p95_s: float) -> float | None:
    """C1: the most one replica sustains inside the SLO with few errors.

    Not the peak throughput. The peak is usually reached with a latency
    distribution nobody would ship, and expressing the autoscaling scenarios
    as multiples of an unusable number would make every one of them a test of
    overload rather than of scaling.
    """
    usable = [
        r
        for r in results
        if r.get("replicas") in (1, None)
        and (r.get("http") or {}).get("error_fraction", 1.0) <= 0.01
        and ((r.get("http") or {}).get("latency") or {}).get("p95_s", 1e9) <= slo_p95_s
    ]
    if not usable:
        return None
    return max(r["http"]["successful_rps"] for r in usable)


def fixed_replica_scaling(run, cfg: dict, dataset: str, entries: list, c1: float) -> list[dict]:
    """Replica counts against offered rates, with both autoscalers off."""
    plan = cfg["scenarios"]["fixed_replica_scaling"]
    timing = cfg["scenarios"]["timing"]
    mix = cfg["scenarios"]["query_mix"]["normal"]
    results: list[dict] = []
    cluster.set_autoscaling(api=False, executor=False)
    for replicas in plan["replicas"]:
        cluster.scale("tap-api", replicas)
        for multiple in plan["c1_multiples"]:
            offered = c1 * multiple
            key = f"repl-{dataset}-n{replicas}-x{multiple}"
            workload = load_mod.Workload(entries, mix, seed=3000 + replicas)
            steps = [
                load_mod.Step(seconds=timing["warmup_seconds"], rate=offered),
                load_mod.Step(seconds=timing["measure_seconds"], rate=offered),
            ]
            result = measure(
                run,
                cfg,
                key=key,
                kind="fixed_replicas",
                dataset=dataset,
                workload=workload,
                mode="open",
                steps=steps,
                replicas=replicas,
                offered_rps=offered,
            )
            if result:
                results.append(result)
            time.sleep(timing["settle_seconds"])
    return results


def keda_scenarios(
    run, cfg: dict, dataset: str, entries: list, c1: float, only: list[str] | None = None
) -> list[dict]:
    """The autoscaling scenarios, against the chart's own ScaledObject.

    Async submission, not sync queries: the repository's ScaledObject scales
    executors on the age of the oldest queued job, so a sync workload — which
    never creates a job — would leave the scaler metric at zero and measure
    nothing at all.
    """
    scenarios = cfg["scenarios"]["keda_scenarios"]
    slo = cfg["scenarios"]["slo"]["p95_seconds"]
    mix = cfg["scenarios"]["query_mix"]["normal"]
    threshold = float(
        cfg["chart_values"]["horizontalAutoscaling"]["tapExecutor"]["backlogSecondsPerReplica"]
    )
    cluster.set_autoscaling(api=False, executor=True)
    config = kube.config_snapshot()
    run.write_json("kubernetes/autoscaler-config.json", config)
    (run.kube_dir / "scaledobject.yaml").write_text(config["scaledobject_yaml"])
    (run.kube_dir / "hpa.yaml").write_text(config["hpa_yaml"])

    out: list[dict] = []
    for scenario in scenarios:
        if only and scenario["id"] not in only:
            continue
        key = f"keda-{scenario['id']}"
        if run.done(key):
            out.append(json.loads((run.path / "state" / f"{key}.done").read_text())["result"])
            continue
        steps = []
        for step in scenario["steps"]:
            if "ramp_to_multiple" in step:
                steps.append(
                    load_mod.Step(
                        seconds=step["seconds"],
                        rate=steps[-1].rate_end or steps[-1].rate if steps else 0.0,
                        rate_end=c1 * step["ramp_to_multiple"],
                    )
                )
            else:
                steps.append(
                    load_mod.Step(seconds=step["seconds"], rate=c1 * step["rate_multiple"])
                )
        workload = load_mod.Workload(entries, mix, seed=4000)
        result = measure(
            run,
            cfg,
            key=key,
            kind="keda",
            dataset=dataset,
            workload=workload,
            mode="open",
            steps=steps,
            offered_rps=None,
            request_mode="async",
        )
        if not result:
            continue

        # The transition of interest is the first step boundary where the rate
        # changes: that is T0.
        elapsed = 0.0
        t0 = result["started_at"]
        for step in steps[:-1]:
            elapsed += step.seconds
            t0 = result["started_at"] + elapsed
            break
        watcher_samples = [
            json.loads(line)
            for line in (run.kube_dir / f"{key}-state.jsonl").read_text().splitlines()
            if line.strip()
        ]
        import pyarrow.parquet as pq

        rows = pq.read_table(run.metrics_dir / f"{key}.parquet").to_pydict()
        metrics_rows = [
            {"metric": m, "labels": lab, "t": t, "value": v}
            for m, lab, t, v in zip(
                rows["metric"], rows["labels"], rows["t"], rows["value"], strict=True
            )
        ]
        samples_rows = pq.read_table(run.samples_dir / f"{key}.parquet").to_pydict()
        from types import SimpleNamespace

        samples = [
            SimpleNamespace(t_start=t, latency_s=lat, status=st, error=err)
            for t, lat, st, err in zip(
                samples_rows["t_start"],
                samples_rows["latency_s"],
                samples_rows["status"],
                samples_rows["error"],
                strict=True,
            )
        ]
        timings = keda_analysis.timings(
            t0=t0,
            metrics_rows=metrics_rows,
            watcher_samples=watcher_samples,
            pod_timings=kube.pod_timings("tap-executor"),
            samples=samples,
            deployment="skao-tap-tap-executor",
            threshold=threshold,
            slo_p95_s=slo,
        )
        behaviour = keda_analysis.scale_behaviour(watcher_samples, "skao-tap-tap-executor")
        entry = {
            "id": scenario["id"],
            "description": scenario["description"],
            "steps": [
                {"seconds": s.seconds, "rate": s.rate, "rate_end": s.rate_end} for s in steps
            ],
            "c1": c1,
            "threshold": threshold,
            "slo_p95_s": slo,
            "t_start": result["started_at"],
            "t_transition": t0,
            "timings": timings,
            "behaviour": behaviour,
            "http": result["http"],
            "bottleneck": result["bottleneck"],
        }
        out.append(entry)
        run.mark_done(key, {"result": entry})
        time.sleep(cfg["scenarios"]["timing"]["settle_seconds"])
    return out


def stress_classes(run, cfg: dict, dataset: str, entries: list) -> list[dict]:
    """Q09/Q11/Q13/Q14 on their own, at modest concurrency.

    Separately because each would otherwise dominate the mixed run's latency
    distribution: one Q14 in a hundred requests moves the p99 of everything
    else and tells you nothing about either.
    """
    results = []
    for query_class in cfg["scenarios"]["query_mix"]["stress_classes"]:
        workload = load_mod.SingleClass(entries, query_class, seed=5000)
        key = f"stress-{dataset}-{query_class}"
        result = measure(
            run,
            cfg,
            key=key,
            kind="stress",
            dataset=dataset,
            workload=workload,
            mode="closed",
            concurrency=4,
            replicas=1,
            warmup_s=15,
            measure_s=60,
        )
        if result:
            results.append(result)
    return results


def capture_plans(run, cfg: dict, entries: list) -> dict:
    """EXPLAIN (ANALYZE, BUFFERS) per class, with no load running."""
    from tapcore.query.adql import adql_to_postgresql

    by_class = corpus_mod.by_class(entries)
    representative = [items[0] for items in by_class.values()]
    # A second entry per parameterised class, so a flag that depends on the
    # parameters (an empty cone versus a full one) has a chance to show up.
    representative += [items[1] for items in by_class.values() if len(items) > 1]
    forward = cluster.port_forward_database()
    try:
        conn = pg_mod.connect(cluster.database_dsn())
        sizes = pg_mod.table_sizes(conn)
        plans = pg_mod.explain(conn, representative, sizes, adql_to_postgresql)
        conn.close()
    finally:
        forward.terminate()
    tally = pg_mod.write_plans(plans, run.explain_dir)
    return tally
