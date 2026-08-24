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
    "tap_executor_cpu_limit_cores": ("tapExecutor", "resources", "limits", "cpu"),
    "postgres_cpu_limit_cores": ("postgresql", "resources", "limits", "cpu"),
    "tap_api_memory_limit_bytes": ("tapApi", "resources", "limits", "memory"),
    "postgres_memory_limit_bytes": ("postgresql", "resources", "limits", "memory"),
}

# Kubernetes memory suffixes. The binary ones are what a chart writes; the
# decimal ones are legal and would otherwise be read as bytes.
_MEMORY_UNITS = {
    "Ki": 1 << 10,
    "Mi": 1 << 20,
    "Gi": 1 << 30,
    "Ti": 1 << 40,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
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


def _memory_bytes(value: typing.Any, default: int) -> int:
    """Kubernetes memory quantity to bytes."""
    if value is None:
        return default
    text = str(value).strip()
    for suffix, factor in _MEMORY_UNITS.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * factor)
    return int(float(text))


def limits(cfg: dict) -> dict:
    values = cfg["chart_values"]

    def dig(path):
        node = values
        for key in path:
            node = (node or {}).get(key)
        return node

    api_cpu = _quantity(dig(LIMITS_FROM_VALUES["tap_api_cpu_limit_cores"]), 2.0)
    exec_cpu = _quantity(dig(LIMITS_FROM_VALUES["tap_executor_cpu_limit_cores"]), 2.0)
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
        # The chart says it plainly: "one executor runs one query at a time".
        # So a pod's ceiling is one core however many the cgroup allows — the
        # same GIL argument as the API's, and the reason the executors read as
        # pinned at 0.96 while their two-core limit looked like headroom.
        "tap_executor_cpu_limit_cores": min(exec_cpu, 1.0),
        "tap_executor_pod_cpu_limit_cores": exec_cpu,
        "postgres_cpu_limit_cores": pg_cpu,
        "db_pool_max_total": pool,
        # Read from the chart rather than restated here. These were constants
        # matching the values file at the time it was written, which is a
        # constant that goes wrong silently: MEMORY_BOUND is judged against
        # them, so a chart whose limit moved would have every memory verdict
        # measured against the old one.
        "tap_api_memory_limit_bytes": _memory_bytes(
            dig(LIMITS_FROM_VALUES["tap_api_memory_limit_bytes"]), 1 << 30
        ),
        "postgres_memory_limit_bytes": _memory_bytes(
            dig(LIMITS_FROM_VALUES["postgres_memory_limit_bytes"]), 6 << 30
        ),
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
    cluster.port_forward_database()
    built = dataset_mod.build(cluster.database_dsn(), cfg["datasets"], targets, out_dir)
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
    response_format: str = "csv",
) -> dict | None:
    """Run one load window and record everything about it."""
    if run.done(key):
        log.info("%s already measured, skipping", key)
        return json.loads((run.path / "state" / f"{key}.done").read_text()).get("result")

    timing = cfg["scenarios"]["timing"]
    warmup_s = timing["warmup_seconds"] if warmup_s is None else warmup_s
    measure_s = timing["measure_seconds"] if measure_s is None else measure_s

    # Every phase is timed, starting here. Set after the setup instead, this
    # missed eight and a half minutes spent establishing a port-forward — the
    # gap showed up only as silence between two log lines, which is the same
    # failure the timing was added to prevent.
    phase_started = time.monotonic()

    def phase(name: str) -> None:
        nonlocal phase_started
        log.info("%s: %s took %.1fs", key, name, time.monotonic() - phase_started)
        phase_started = time.monotonic()

    prometheus = prom_mod.Prometheus(PROMETHEUS_URL)
    cluster.port_forward_database()
    conn = pg_mod.connect(cluster.database_dsn())
    guard = guards_mod.Guards(min_free_disk_gb=cfg["hardware"]["enforcement"]["min_free_disk_gb"])
    phase("setup")

    try:
        pg_mod.reset_statements(conn)
        before = pg_mod.snapshot(conn)
        activity_samples = [pg_mod.activity(conn)]
        phase("statistics snapshot")

        measured_elapsed = float(measure_s)
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
                        response_format=response_format,
                        max_in_flight=int(
                            timing.get(
                                "max_in_flight_async"
                                if request_mode == "async"
                                else "max_in_flight",
                                4096,
                            )
                        ),
                    )
                )
                measured_elapsed = time.time() - started
            else:
                recorder, measured_elapsed = asyncio.run(
                    load_mod.closed_loop(
                        BASE_URL,
                        workload,
                        concurrency or 1,
                        warmup_s,
                        measure_s,
                        mode=request_mode,
                        response_format=response_format,
                    )
                )
                timeline = []
            window_end = time.time()
        phase("load")
        activity_samples.append(pg_mod.activity(conn))
        after = pg_mod.snapshot(conn)
        statements = pg_mod.statements(conn)
    finally:
        conn.close()

    # The measured phase as it actually ran, not as it was requested: a worker
    # only checks the clock between requests, so a phase can overrun, and
    # dividing by the requested duration would overstate throughput by exactly
    # the overrun. Prometheus is queried for that same span, so a cold cache
    # during warmup cannot leak into the resource figures.
    measured_from = window_end - measured_elapsed
    metrics_rows, coverage = prometheus.collect(measured_from - 2, window_end + 2)
    prometheus.close()
    phase("prometheus collection")

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

    dropped_arrivals = sum(e.get("dropped_arrivals", 0) for e in timeline)
    guard_results = guard.evaluate(
        recorder=recorder,
        prometheus_report=coverage,
        pod_timings=pod_timings,
        metrics_rows=metrics_rows,
        dropped_arrivals=dropped_arrivals,
    )
    failed = [r for r in guard_results if not r.ok]
    for failure in failed:
        run.invalidate(f"{key}: {failure.name}", {"detail": failure.detail, **failure.measured})

    limits_map = limits(cfg)
    # The per-pod ceilings go in as they are: classify() multiplies each of
    # them by the replicas the run actually had ready, which is the only count
    # that is also right when an autoscaler moved it mid-window. The same
    # count is what the reported CPU fraction has to be against.
    api_limit = limits_map["tap_api_cpu_limit_cores"] * bottleneck.fleet_replicas(metrics_rows)
    verdicts = bottleneck.classify(
        metrics_rows=metrics_rows,
        summary=http,
        pg_summary=pg_summary,
        recorder_cpu_peak=recorder.generator_cpu_peak,
        limits=limits_map,
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
        # Which writer produced the bytes. Recorded on every measurement, not
        # only the ones that vary it: the suite defaulted to CSV silently, so
        # every published latency was a CSV latency and nothing said so.
        "response_format": response_format,
        "window_seconds": window,
        "measured_phase_overrun_s": measured_elapsed - float(measure_s),
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
        # Alongside the lateness above, because the two are the same question
        # asked of the arrivals that went out late and the arrivals that never
        # went out at all. Only the first is visible in the latencies.
        "arrivals_dropped": dropped_arrivals,
        "unoffered_fraction": dropped_arrivals / max(len(recorder.samples) + dropped_arrivals, 1),
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
    usable = [r for r in results if r.get("replicas") in (1, None) and _within_slo(r, slo_p95_s)]
    if not usable:
        return None
    return max(r["http"]["successful_rps"] for r in usable)


def _within_slo(result: dict, slo_p95_s: float) -> bool:
    """A measurement the service passed, and that is a measurement at all."""
    if result.get("invalid"):
        return False
    http = result.get("http") or {}
    return (
        http.get("error_fraction", 1.0) <= 0.01
        and (http.get("latency") or {}).get("p95_s", 1e9) <= slo_p95_s
    )


def bracketed_capacity(
    results: list[dict], *, kind: str, replicas: int, slo_p95_s: float
) -> dict | None:
    """The most a replica count sustains, and whether that is its ceiling.

    A capacity is only a capacity if the ladder went past it: the highest rate
    the service met says nothing about where it stops unless some *valid*
    higher rung is known to have failed. Without that, the number is the
    largest rate anybody happened to offer.

    This matters because the ratio of two such numbers looks exactly like a
    scaling efficiency. This run offered one replica 115 rps and eight
    replicas 231 rps, both met in full with a p95 under 60 ms, and the ratio
    would have been published as 25% scaling efficiency at eight replicas —
    a claim about a ceiling neither point came near.
    """
    at = [r for r in results if r.get("kind") == kind and r.get("replicas") == replicas]
    met = [r for r in at if _within_slo(r, slo_p95_s)]
    if not met:
        return None
    best = max(met, key=lambda r: r["http"]["successful_rps"])
    offered = best.get("offered_rps") or 0.0
    # A higher rung that the service failed, rather than one the generator
    # failed to offer: an invalid point brackets nothing.
    above = [
        r
        for r in at
        if (r.get("offered_rps") or 0.0) > offered
        and not r.get("invalid")
        and not _within_slo(r, slo_p95_s)
    ]
    return {
        "rps": best["http"]["successful_rps"],
        "offered_rps": offered,
        "key": best["key"],
        "bracketed": bool(above),
        "next_rung_measured": bool(
            [r for r in at if (r.get("offered_rps") or 0.0) > offered and not r.get("invalid")]
        ),
    }


def _pods(n: int) -> str:
    return "one replica" if n == 1 else f"{n} replicas"


def keda_timings_from_artefacts(run_dir, entry: dict, key: str, metrics_rows: list[dict]) -> dict:
    """A KEDA scenario's stage timings, re-derived from what the run stored.

    The stamps come from four sources — the scaler's series, the HPA's, the
    Pods' lifecycle and the request samples — and all four are written to the
    run directory. So a correction to how the stages are computed can reach the
    scenarios that were already measured, which is the whole reason the
    artefacts are kept.
    """
    import json
    import pathlib
    from types import SimpleNamespace

    import pyarrow.parquet as pq

    run_dir = pathlib.Path(run_dir)
    state = run_dir / "kubernetes" / f"{key}-state.jsonl"
    pods = run_dir / "kubernetes" / f"{key}-pods.json"
    sample_path = run_dir / "samples" / f"{key}.parquet"
    if not (state.exists() and pods.exists() and sample_path.exists()):
        return {}
    if entry.get("t_transition") is None or entry.get("threshold") is None:
        return {}
    watcher = [json.loads(line) for line in state.read_text().splitlines() if line.strip()]
    # measure() records both deployments' Pods; the executors are the ones the
    # ScaledObject moves.
    pod_timings = [p for p in json.loads(pods.read_text()) if p.get("component") == "tap-executor"]
    columns = pq.read_table(
        sample_path, columns=["t_start", "latency_s", "status", "error"]
    ).to_pydict()
    samples = [
        SimpleNamespace(t_start=t, latency_s=lat, status=st, error=err)
        for t, lat, st, err in zip(
            columns["t_start"],
            columns["latency_s"],
            columns["status"],
            columns["error"],
            strict=True,
        )
    ]
    return keda_analysis.timings(
        t0=entry["t_transition"],
        metrics_rows=metrics_rows,
        watcher_samples=watcher,
        pod_timings=pod_timings,
        samples=samples,
        deployment="skao-tap-tap-executor",
        threshold=float(entry["threshold"]),
        slo_p95_s=float(entry.get("slo_p95_s") or 2.0),
        scale_up=keda_analysis.scaling_up(entry.get("steps") or []),
    )


def _lateness_from_samples(run_dir, key: str) -> dict:
    """Arrival lateness rebuilt from a measurement's stored samples.

    For measurements saved without their `coordinated_omission` block — the
    KEDA scenarios were trimmed to their scenario fields before being written —
    the per-request offered and start times are still in the samples Parquet,
    so the lateness the guard needs can be recovered rather than re-measured.
    """
    import pathlib

    import pyarrow.parquet as pq

    path = pathlib.Path(run_dir) / "samples" / f"{key}.parquet"
    if not path.exists():
        return {}
    table = pq.read_table(path, columns=["t_start", "t_offered"]).to_pydict()
    late = sorted(
        start - offered
        for start, offered in zip(table["t_start"], table["t_offered"], strict=True)
        if offered and offered > 0
    )
    if not late:
        return {}
    return {
        "p95_lateness_s": late[int(0.95 * (len(late) - 1))],
        "max_lateness_s": late[-1],
        "samples": len(late),
        "recovered_from_samples": True,
    }


def replica_capacities(results: list[dict], slo_p95_s: float) -> list[dict]:
    """Per replica count: the highest rate met, and whether it is a ceiling.

    Written into the summary so the report renders what the analysis decided
    rather than deciding it again from the rows — the per-replica efficiency
    column is the same claim as the headline and must not be able to disagree
    with it.
    """
    counts = sorted(
        {
            r["replicas"]
            for r in results
            if r.get("kind") == "fixed_replicas" and r.get("replicas") is not None
        }
    )
    out = []
    for n in counts:
        found = bracketed_capacity(results, kind="fixed_replicas", replicas=n, slo_p95_s=slo_p95_s)
        if found:
            out.append({"replicas": n, **found})
    return out


def capacity_headline(results: list[dict], slo_p95_s: float) -> dict:
    """C1 and replica scaling efficiency, each qualified by what bracketed it.

    Only a ratio of two ceilings is an efficiency, and only a rate the service
    was pushed past is a capacity. Where the ladder bracketed neither, the
    entry says so — no number for the efficiency, an explicit "lower bound" on
    C1 — because a headline carrying a null gets read and a missing headline
    does not.
    """
    out: dict = {}
    c1 = sustainable_capacity(results, slo_p95_s)
    if c1:
        # Qualified only when the evidence for C1 is an offered-rate ladder that
        # never failed. A concurrency sweep is bracketed by construction — it
        # stops when two saturation signals agree — so its C1 needs no caveat,
        # and attaching one would be its own false statement.
        at_one = bracketed_capacity(results, kind="fixed_replicas", replicas=1, slo_p95_s=slo_p95_s)
        qualifier = (
            " — a lower bound, since no valid higher rate failed"
            if at_one and not at_one["bracketed"]
            else ""
        )
        out["sustainable single-replica capacity (C1)"] = {
            "value": round(c1, 1),
            "evidence": "highest successful rps over valid measurements with p95 within "
            f"the {slo_p95_s}s SLO and errors under 1%{qualifier}",
        }
    for kind, label, top_n in (("fixed_replicas", "replica scaling efficiency at 8", 8),):
        one = bracketed_capacity(results, kind=kind, replicas=1, slo_p95_s=slo_p95_s)
        top = bracketed_capacity(results, kind=kind, replicas=top_n, slo_p95_s=slo_p95_s)
        if not (one and top):
            continue
        if one["bracketed"] and top["bracketed"] and one["rps"]:
            out[label] = {
                "value": round(top["rps"] / (top_n * one["rps"]), 3),
                "evidence": f"{top['rps']:.1f} rps on {_pods(top_n)} against "
                f"{one['rps']:.1f} on one, each the last rate met before a "
                "measured failure",
            }
            continue
        open_ended = [
            f"{_pods(n)} served every one of the {c['offered_rps']:.0f} rps offered ({c['key']})"
            for n, c in ((1, one), (top_n, top))
            if not c["bracketed"]
        ]
        out[label] = {
            "value": None,
            "evidence": "not determined: "
            + "; ".join(open_ended)
            + ". An efficiency needs a ceiling at each replica count, and no valid "
            "higher rung failed — every rung above is either unmeasured or was "
            "marked invalid.",
        }
    return out


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
    executors on the number of queued jobs, so a sync workload — which
    never creates a job — would leave the scaler metric at zero and measure
    nothing at all.
    """
    scenarios = cfg["scenarios"]["keda_scenarios"]
    slo = cfg["scenarios"]["slo"]["p95_seconds"]
    mix = cfg["scenarios"]["query_mix"]["normal"]
    threshold = float(
        cfg["chart_values"]["horizontalAutoscaling"]["tapExecutor"]["queuedJobsPerReplica"]
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
        step_records = [
            {"seconds": s.seconds, "rate": s.rate, "rate_end": s.rate_end} for s in steps
        ]
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
            scale_up=keda_analysis.scaling_up(step_records),
        )
        behaviour = keda_analysis.scale_behaviour(watcher_samples, "skao-tap-tap-executor")
        # Classified again with the stage breakdown, which does not exist until
        # here. KEDA_SCALE_LAG is the one class this family is for and
        # classify() cannot reach it without the timings, so a scenario whose
        # latency was entirely the scaling delay came out UNKNOWN — 566-second
        # p95 on a fleet where nothing was saturated, and no name for it.
        verdicts = bottleneck.classify(
            metrics_rows=metrics_rows,
            summary=result.get("http") or {},
            pg_summary=result.get("postgres") or {},
            recorder_cpu_peak=result.get("generator_cpu_peak") or 0.0,
            limits=limits(cfg),
            keda=timings,
        )
        result["bottleneck"] = [
            {
                "classification": v.classification,
                "confidence": v.confidence,
                "evidence": v.evidence,
                "explanation": v.explanation,
            }
            for v in verdicts
        ]
        # The scenario's own analysis *on top of* the measurement, not instead
        # of it. Listing the fields to keep dropped the guards, the `invalid`
        # flag and the coordinated-omission block, so a KEDA scenario could not
        # be marked invalid however badly the run had gone: the guards were
        # computed and then thrown away before anything read them.
        entry = {
            **result,
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


def shedding(run, cfg: dict, dataset: str, entries: list) -> list[dict]:
    """Package 13: hold a bounded-concurrency overload and watch how the
    excess is refused.

    A closed loop *is* bounded concurrency — each of its N clients has
    exactly one request outstanding — so holding N far past saturation is
    the sustained-overload point the open-loop generator cannot keep still
    (it either abandons arrivals at its in-flight cap or grows without
    bound). The ladder climbs through the accept backlog (2,048 by default)
    because the first hypothesis for the resets is the listen queue
    overflowing before the application ever sees the connection.

    Two passes per replica count: the ceiling off, to find where resets
    begin; then `tapApi.limitConcurrency` set to the configured value, to
    show the same held load being refused with 503s instead of dropped at
    the socket. The flip goes through the chart (a helm upgrade), which
    resets the replica count, so the scale call follows every flip.
    """
    plan = cfg["scenarios"]["shedding"]
    mix = cfg["scenarios"]["query_mix"]["normal"]
    settle_s = cfg["scenarios"]["timing"]["settle_seconds"]
    results: list[dict] = []
    cluster.set_autoscaling(api=False, executor=False)
    try:
        for limited, limit in ((False, 0), (True, int(plan["limit_concurrency"]))):
            cluster.set_limit_concurrency(limit)
            for replicas in plan["replicas"]:
                cluster.scale("tap-api", replicas)
                for concurrency in plan["held_concurrency"]:
                    label = f"limit{limit}" if limited else "open"
                    key = f"shed-{dataset}-n{replicas}-{label}-c{concurrency}"
                    # A fresh workload per measurement: every point on the
                    # ladder replays the same request sequence, so the error
                    # mix is attributable to the held concurrency alone.
                    workload = load_mod.Workload(entries, mix, seed=9000 + replicas)
                    result = measure(
                        run,
                        cfg,
                        key=key,
                        kind="shedding",
                        dataset=dataset,
                        workload=workload,
                        mode="closed",
                        concurrency=concurrency,
                        replicas=replicas,
                        warmup_s=plan["warmup_seconds"],
                        measure_s=plan["measure_seconds"],
                    )
                    if result:
                        results.append(result)
                    time.sleep(settle_s)
    finally:
        # hand the next family the chart's defaults back
        cluster.set_limit_concurrency(0)
        cluster.scale("tap-api", 1)
    return results


def shedding_summary(results: list[dict]) -> list[dict]:
    """The shedding ladder reduced to the numbers the package asked for:
    per held concurrency, how much of the shed load was an answer (503)
    and how much was a socket drop (ReadError and friends)."""
    rows: list[dict] = []
    for result in results:
        if result.get("kind") != "shedding":
            continue
        http = result["http"]
        errors = dict(http.get("errors_by_type") or {})
        resets = int(errors.pop("ReadError", 0))
        statuses = {str(k): int(v) for k, v in (http.get("errors_by_status") or {}).items()}
        rows.append(
            {
                "key": result["key"],
                "replicas": result.get("replicas"),
                "held_concurrency": result.get("concurrency"),
                "requests": int(http.get("requests", 0)),
                "rps": float(http.get("rps", 0.0)),
                "refused_503": statuses.get("503", 0),
                "reset_readerror": resets,
                "other_errors": {k: v for k, v in errors.items() if v},
            }
        )
    rows.sort(key=lambda r: (r["key"].split("-c")[0], r["held_concurrency"] or 0))
    return rows


def result_formats(run, cfg: dict, dataset: str, entries: list) -> list[dict]:
    """One query class, one writer, everything else held still.

    Package 10's question is what a large result costs to *produce*, which the
    rest of the suite could not answer: every measurement it ever took went
    out as CSV, because that was the load generator's default and nothing
    varied it. Rows found, rows fetched and rows counted are identical across
    the formats here, so the difference between two of these measurements is
    the writer and nothing else.

    Repeated, because the differences worth acting on are tens of
    milliseconds and one window's page cache is worth more than that.
    """
    settings = cfg["scenarios"]["result_formats"]
    results: list[dict] = []
    for query_class in settings["query_classes"]:
        for fmt in settings["formats"]:
            for repetition in range(settings["repetitions"]):
                result = measure(
                    run,
                    cfg,
                    key=f"format-{dataset}-{query_class}-{fmt}-r{repetition}",
                    kind="format",
                    dataset=dataset,
                    # A fresh workload per measurement, not one shared across
                    # the formats. `SingleClass` holds a counter-based PRNG, so
                    # a shared instance hands the second format the sequence
                    # the first one left off at — the writers would then be
                    # compared on different queries, which is the one thing
                    # this family exists not to do.
                    workload=load_mod.SingleClass(entries, query_class, seed=7000),
                    mode="closed",
                    concurrency=settings["concurrency"],
                    replicas=1,
                    repetition=repetition,
                    # Warmed once per format rather than once per class: the
                    # page cache is shared, but the writer's own code paths and
                    # the pyarrow import are not.
                    warmup_s=settings["warmup_seconds"] if repetition == 0 else 0,
                    measure_s=settings["measure_seconds"],
                    response_format=fmt,
                )
                if result:
                    results.append(result)
    return results


def format_comparison(results: list[dict]) -> list[dict]:
    """Per (class, format): the cost of a row, and of a byte.

    Latency alone does not separate the two things a writer can be expensive
    at, so both are reported: seconds per row is what the writer costs to
    produce a cell, and bytes per row is what the client then has to receive.
    A format can win on one and lose on the other, and Parquet does.
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    for result in results:
        if result.get("kind") != "format":
            continue
        for query_class, summary in (result.get("by_class") or {}).items():
            grouped.setdefault((query_class, result["response_format"]), []).append(summary)

    rows: list[dict] = []
    for (query_class, fmt), summaries in sorted(grouped.items()):
        requests = sum(s.get("requests") or 0 for s in summaries)
        if not requests:
            continue
        # Weighted by requests, and the p95 taken as the worst repetition
        # rather than an average of percentiles — averaging percentiles is not
        # a percentile of anything.
        p95 = max((s.get("latency") or {}).get("p95_s") or 0.0 for s in summaries)
        p50 = max((s.get("latency") or {}).get("p50_s") or 0.0 for s in summaries)
        rps = sum(s.get("rps") or 0.0 for s in summaries) / len(summaries)
        mean_bytes = (
            sum((s.get("mean_response_bytes") or 0.0) * (s.get("requests") or 0) for s in summaries)
            / requests
        )
        rows.append(
            {
                "query_class": query_class,
                "response_format": fmt,
                "repetitions": len(summaries),
                "requests": requests,
                "rps": rps,
                "latency_p50_s": p50,
                "latency_p95_s": p95,
                "mean_response_bytes": mean_bytes,
            }
        )
    return rows


def capture_plans(run, cfg: dict, entries: list) -> dict:
    """EXPLAIN (ANALYZE, BUFFERS) per class, with no load running."""
    from tapcore.query.adql import adql_to_postgresql

    by_class = corpus_mod.by_class(entries)
    representative = [items[0] for items in by_class.values()]
    # A second entry per parameterised class, so a flag that depends on the
    # parameters (an empty cone versus a full one) has a chance to show up.
    representative += [items[1] for items in by_class.values() if len(items) > 1]
    cluster.port_forward_database()
    conn = pg_mod.connect(cluster.database_dsn())
    try:
        sizes = pg_mod.table_sizes(conn)
        plans = pg_mod.explain(conn, representative, sizes, adql_to_postgresql)
    finally:
        conn.close()
    tally = pg_mod.write_plans(plans, run.explain_dir)
    return tally


def reclassify(run_dir, cfg: dict) -> dict:
    """Recompute the derived analysis of a finished run from its artefacts.

    Every measurement keeps its PostgreSQL snapshots, its deltas and its
    Prometheus series, precisely so that a mistake in the analysis can be
    corrected without re-measuring anything. This is that path: it re-derives
    the database summary and the bottleneck classification and leaves the
    measurements — samples, percentiles, timings — untouched.

    The previous summary is kept beside the new one rather than replaced. A run
    directory is append-only by design, and "the analysis changed" is exactly
    the kind of thing a reader needs to be able to see.
    """
    import json
    import pathlib
    import shutil
    import time as _time

    import pyarrow.parquet as pq

    run_dir = pathlib.Path(run_dir)
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text())
    shutil.copy2(summary_path, run_dir / f"summary.superseded-{int(_time.time())}.json")

    limits_map = limits(cfg)
    # Guard failures the run itself recorded, keyed by measurement. These are
    # the floor: re-judging may add a failure but must never clear one whose
    # input the summary no longer carries, or a rerun of the analysis would
    # quietly pronounce a rejected measurement sound. They also carry the
    # numbers the verdict was reached on, which is how a scenario saved without
    # its load timeline can still be re-judged on abandoned arrivals.
    recorded: dict[str, dict[str, dict]] = {}
    invalid_path = run_dir / "invalid.json"
    if invalid_path.exists():
        for reason in json.loads(invalid_path.read_text()).get("reasons") or []:
            text = str(reason.get("reason") or "")
            measurement, _, guard = text.partition(": ")
            if measurement and guard:
                recorded.setdefault(measurement, {})[guard.strip()] = reason

    changed = []
    # The autoscaling scenarios are analysis like any other and were being
    # skipped: they live under "keda" rather than "runs", so a correction to the
    # rules reached every family except the one the run was for. They key off
    # `id` rather than `key`.
    measurements = [(e, e["key"]) for e in summary.get("runs") or []] + [
        (e, e.get("key") or f"keda-{e['id']}") for e in summary.get("keda") or []
    ]
    for entry, key in measurements:
        delta_path = run_dir / "postgres" / f"{key}-delta.json"
        metrics_path = run_dir / "metrics" / f"{key}.parquet"
        if not delta_path.exists():
            continue
        pg_summary = pg_mod.summarise(json.loads(delta_path.read_text()))
        metrics_rows = []
        if metrics_path.exists():
            table = pq.read_table(metrics_path).to_pydict()
            metrics_rows = [
                {"metric": m, "labels": lab, "t": t, "value": v}
                for m, lab, t, v in zip(
                    table["metric"],
                    table["labels"],
                    table["t"],
                    table["value"],
                    strict=True,
                )
            ]
        # Timings before the classification, because the classification needs
        # them: KEDA_SCALE_LAG is the one class this family exists to find and
        # it cannot fire without the stage breakdown.
        if "timings" in entry:
            redone = keda_timings_from_artefacts(run_dir, entry, key, metrics_rows)
            if redone:
                entry["timings"] = redone
        verdicts = bottleneck.classify(
            metrics_rows=metrics_rows,
            summary=entry.get("http") or {},
            pg_summary=pg_summary,
            recorder_cpu_peak=entry.get("generator_cpu_peak") or 0.0,
            limits=limits_map,
            keda=entry.get("timings"),
        )
        # The two client-fidelity rules are re-judged here as well: a rule
        # written after a run can still be applied to it, and a run measured
        # before either rule existed is exactly the run that needs them.
        omission = entry.get("coordinated_omission") or _lateness_from_samples(run_dir, key)
        was_recorded = recorded.get(key, {})
        dropped = entry.get("arrivals_dropped")
        if dropped is None and "load_timeline" in entry:
            dropped = sum(e.get("dropped_arrivals", 0) for e in entry["load_timeline"] or [])
        if dropped is None:
            # Not in the summary, but the run wrote the count into the failure
            # it recorded at the time.
            dropped = was_recorded.get("load_generator_offered_the_rate", {}).get(
                "arrivals_dropped"
            )
        issued = int(omission.get("samples") or (entry.get("http") or {}).get("requests") or 0)
        if dropped is not None:
            entry["arrivals_dropped"] = dropped
            entry["unoffered_fraction"] = dropped / max(issued + dropped, 1)
        schedule = guards_mod.schedule_verdicts(
            lateness_p95_s=omission.get("p95_lateness_s"),
            lateness_max_s=omission.get("max_lateness_s"),
            arrivals_dropped=dropped or 0,
            arrivals_issued=issued,
        )
        judged = {v.name for v in schedule}
        kept = [g for g in entry.get("guards") or [] if g["name"] not in judged]
        guard_records = kept + [{"name": v.name, "ok": v.ok, "detail": v.detail} for v in schedule]
        # A failure the run recorded and this pass cannot re-derive is carried
        # forward rather than dropped. Re-running the analysis must not be able
        # to turn a rejected measurement into an accepted one by forgetting why
        # it was rejected.
        known = {g["name"] for g in guard_records}
        guard_records += [
            {"name": name, "ok": False, "detail": reason.get("detail", "recorded during the run")}
            for name, reason in was_recorded.items()
            if name not in known
        ]
        # Only claim validity where something judged it. An entry with no guards
        # at all — the shape the KEDA scenarios were saved in — must not come
        # out of here asserted valid on no evidence.
        if guard_records:
            entry["guards"] = guard_records
            entry["invalid"] = any(not g["ok"] for g in guard_records)

        was = (entry.get("bottleneck") or [{}])[0].get("classification")
        entry["postgres"] = pg_summary
        # Re-derived with the verdicts, because it shares their denominator.
        resources = entry.get("resources") or {}
        if resources:
            fleet = limits_map["tap_api_cpu_limit_cores"] * bottleneck.fleet_replicas(metrics_rows)
            resources["tap_api_cpu_fraction_of_limit"] = (
                resources.get("tap_api_cpu_cores_p95", 0.0) / fleet
            )
        entry["bottleneck"] = [
            {
                "classification": v.classification,
                "confidence": v.confidence,
                "evidence": v.evidence,
                "explanation": v.explanation,
            }
            for v in verdicts
        ]
        now = verdicts[0].classification if verdicts else None
        if was != now:
            changed.append((key, was, now))

    tally: dict[str, dict] = {}
    for entry, _ in measurements:
        for verdict in entry.get("bottleneck") or []:
            slot = tally.setdefault(
                verdict["classification"],
                {"count": 0, "explanation": verdict["explanation"]},
            )
            slot["count"] += 1
    summary["bottleneck_tally"] = tally
    # The headline is derived from validity, so re-judging validity has to
    # re-derive it. Leaving it would keep publishing a scaling efficiency that
    # the reclassified measurements no longer support.
    entries = summary.get("runs") or []
    slo_p95_s = cfg["scenarios"]["slo"]["p95_seconds"]
    headline = summary.get("headline") or {}
    headline.update(capacity_headline(entries, slo_p95_s))
    summary["headline"] = headline
    summary["replica_capacity"] = replica_capacities(entries, slo_p95_s)
    summary["sustainable_capacity_c1"] = sustainable_capacity(entries, slo_p95_s)
    summary["reanalysed_at"] = _time.time()
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str, allow_nan=False)
    )
    for key, was, now in changed:
        log.info("reclassified %s: %s -> %s", key, was, now)
    # Over everything re-judged, not just `runs` — counting the numerator over
    # both lists and the denominator over one printed "4 of 3".
    log.info("%d of %d measurements changed classification", len(changed), len(measurements))
    return summary
