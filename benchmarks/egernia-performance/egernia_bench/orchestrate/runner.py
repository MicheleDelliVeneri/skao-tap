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
import contextlib
import dataclasses
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
from ..collect import oidc as oidc_mod
from ..collect import postgres as pg_mod
from ..collect import prometheus as prom_mod
from ..collect import pyspy as pyspy_mod
from ..dataset import generate as dataset_mod
from ..load import runner as load_mod

log = logging.getLogger("egernia_bench.orchestrate")

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

# The error classes (load/runner.py records `type(exc).__name__`) that mean
# the connection died without the application answering. Package 13's claim
# is "shed with 503s, not drops", and counting only ReadError made that claim
# about one of the four ways the same reset arrives: which one a client sees
# depends on where in the exchange the socket went — ConnectError before the
# handshake completes, ConnectionResetError when the kernel resets a queued
# connection, ReadError mid-response, RemoteProtocolError when the peer
# vanishes leaving a truncated HTTP frame. A timeout is NOT here: it means
# the server held the connection and was too slow, which is a different
# failure and stays visible in other_errors.
TRANSPORT_DROP_ERRORS = (
    "ConnectError",
    "ConnectionResetError",
    "ReadError",
    "RemoteProtocolError",
)

# Fallback for the analysis functions when no config is in hand (a test, a
# re-analysis of a summary written before the field existed). It mirrors
# config/scenarios.yaml's concurrency_sweep.saturation_signals_required, and
# the only reason it is not read from there is that these functions take
# `results`, not `cfg`. Every production caller passes the configured value.
SATURATION_SIGNALS_REQUIRED_DEFAULT = 2


def saturation_signals_required(cfg: dict) -> int:
    """How many agreeing signals the sweep stopped climbing on.

    The same number decides where the ladder stopped and whether the analysis
    calls the stop a ceiling. Raise it in the config and only the ladder would
    have moved: the analysis would keep calling two signals a measured ceiling
    for a sweep that climbed straight past them.
    """
    sweep = (cfg.get("scenarios") or {}).get("concurrency_sweep") or {}
    return int(sweep.get("saturation_signals_required") or SATURATION_SIGNALS_REQUIRED_DEFAULT)


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
    pool_timeout = float((values.get("config") or {}).get("dbPoolTimeoutSeconds") or 5.0)
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
        # what a pool wait is graded against: a wait can never legitimately
        # exceed this, so a longer one is a measurement artefact
        "db_pool_timeout_s": pool_timeout,
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


def limits_with_workers(
    limits_map: dict,
    workers: int | None,
    *,
    pod_cpu: float | None = None,
    pod_memory_bytes: int | None = None,
) -> dict:
    """The per-pod ceilings for the pod actually deployed.

    The worker sweep deploys counts the values file does not state, and
    `limits()` reads the file. Grading those measurements against the file's
    count would repeat the mistake the min(cpu, workers) ceiling exists to
    prevent: a two-worker pod judged against a one-worker ceiling reads as
    past its limit at half load, and a four-worker pod's pool arithmetic
    would be counted at a quarter of its real connections. The limit probe
    also raises the pod's CPU and memory limits past the file's, for the
    same reason in the other direction: a probe pod graded against the
    file's limits would read as impossibly past them.
    """
    if (
        pod_cpu is None
        and pod_memory_bytes is None
        and (workers is None or workers == limits_map.get("tap_api_workers"))
    ):
        return limits_map
    out = dict(limits_map)
    if pod_cpu is not None:
        out["tap_api_pod_cpu_limit_cores"] = pod_cpu
    if pod_memory_bytes is not None:
        out["tap_api_memory_limit_bytes"] = pod_memory_bytes
    if workers is not None:
        out["tap_api_workers"] = workers
    out["tap_api_cpu_limit_cores"] = min(
        out["tap_api_pod_cpu_limit_cores"], float(out["tap_api_workers"])
    )
    return out


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


def warmup_for(cfg: dict, dataset: str) -> float | None:
    """The tier's own warmup, if it declares one.

    The 60-second default demonstrably does not warm the larger tiers (the
    first repetition at each size was colder than the rest), so a tier whose
    working set exceeds memory states how long reaching its steady-state hit
    ratio takes. None means "use the timing default".
    """
    for entry in cfg["datasets"]["datasets"]:
        if entry["name"] == dataset and "warmup_seconds" in entry:
            return float(entry["warmup_seconds"])
    return None


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
    workers: int | None = None,
    pod_cpu_limit: float | None = None,
    pod_memory_limit_bytes: int | None = None,
    offered_rps: float | None = None,
    repetition: int = 0,
    warmup_s: float | None = None,
    measure_s: float | None = None,
    request_mode: str = "sync",
    response_format: str = "csv",
    generator_processes: int = 1,
    workload_ingredients: dict | None = None,
    bearer_token: str | None = None,
    during_load: contextlib.AbstractContextManager | None = None,
) -> dict | None:
    """Run one load window and record everything about it.

    ``bearer_token`` makes this an authenticated measurement: the generator
    presents it on every request, and the service verifies it on every
    request. Recorded on the result, because "98 rps" means two different
    things with and without it and nothing in the artefacts said which one a
    figure was.

    ``during_load`` is entered around the load phase and exited when it
    finishes — where a profiler belongs, since a py-spy pass has to cover the
    measured window and nothing else. It is not entered at all when the
    measurement is skipped as already done, so a caller reading results off it
    must cope with an empty one.
    """
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
        # The restart baseline, so the guard judges restarts *during* this
        # window rather than the pods' whole history: restartCount is
        # cumulative, and a pod that crashed once before a sweep would
        # otherwise invalidate every later measurement it appears in.
        restarts_before = {
            p["pod"]: p.get("restarts") or 0
            for p in kube.pod_timings("tap-api") + kube.pod_timings("tap-executor")
        }
        phase("statistics snapshot")

        measured_elapsed = float(measure_s)
        watcher_path = run.kube_dir / f"{key}-state.jsonl"
        started = time.time()
        with kube.StateWatcher(watcher_path), during_load or contextlib.nullcontext():
            if steps is not None:
                max_in_flight = int(
                    timing.get(
                        "max_in_flight_async" if request_mode == "async" else "max_in_flight",
                        4096,
                    )
                )
                if generator_processes > 1:
                    # sharded for the same reason as the closed loop below —
                    # and the async scenarios need it at even modest offered
                    # rates, because every queued job holds a phase-poll loop
                    assert workload_ingredients is not None
                    recorder, timeline, measured_elapsed = load_mod.open_loop_sharded(
                        BASE_URL,
                        steps=steps,
                        processes=generator_processes,
                        mode=request_mode,
                        response_format=response_format,
                        max_in_flight=max_in_flight,
                        token=bearer_token,
                        **workload_ingredients,
                    )
                else:
                    recorder, timeline = asyncio.run(
                        load_mod.open_loop(
                            BASE_URL,
                            workload,
                            steps,
                            mode=request_mode,
                            response_format=response_format,
                            max_in_flight=max_in_flight,
                            token=bearer_token,
                        )
                    )
                    measured_elapsed = time.time() - started
            elif generator_processes > 1:
                # sharded across processes: one asyncio loop tops out around
                # one core, and the fleet under test serves more than that
                assert workload_ingredients is not None
                recorder, measured_elapsed = load_mod.closed_loop_sharded(
                    BASE_URL,
                    concurrency=concurrency or 1,
                    warmup_s=warmup_s,
                    measure_s=measure_s,
                    processes=generator_processes,
                    mode=request_mode,
                    response_format=response_format,
                    token=bearer_token,
                    **workload_ingredients,
                )
                timeline = []
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
                        token=bearer_token,
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
    prom_mod.write(metrics_rows, prometheus_parquet)
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
    routing = stats_mod.served_by(recorder.samples)
    resources = aggregate_resources(metrics_rows)
    pg_summary = pg_mod.summarise(delta)

    dropped_arrivals = sum(e.get("dropped_arrivals", 0) for e in timeline)
    guard_results = guard.evaluate(
        recorder=recorder,
        prometheus_report=coverage,
        pod_timings=pod_timings,
        metrics_rows=metrics_rows,
        dropped_arrivals=dropped_arrivals,
        restarts_before=restarts_before,
    )
    failed = [r for r in guard_results if not r.ok]
    for failure in failed:
        run.invalidate(f"{key}: {failure.name}", {"detail": failure.detail, **failure.measured})

    # The worker sweep deploys a count the values file does not state; every
    # ceiling below has to be the deployed pod's, not the file's.
    limits_map = limits_with_workers(
        limits(cfg), workers, pod_cpu=pod_cpu_limit, pod_memory_bytes=pod_memory_limit_bytes
    )
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
        # None means "whatever the values file says" — only the worker sweep
        # varies it, and a recorded 1 on every older measurement would claim a
        # certainty about deployments this field did not exist to observe.
        "workers": workers,
        # Same convention: recorded only where a measurement deployed a limit
        # the values file does not state (the limit probe).
        "pod_cpu_limit": pod_cpu_limit,
        "offered_rps": offered_rps,
        "repetition": repetition,
        "request_mode": request_mode,
        # Which writer produced the bytes. Recorded on every measurement, not
        # only the ones that vary it: the suite defaulted to CSV silently, so
        # every published latency was a CSV latency and nothing said so.
        "response_format": response_format,
        # Whether the requests carried a verified bearer token. Every family
        # but the profile one measures an unauthenticated service, and for a
        # long time nothing said so.
        "authenticated": bool(bearer_token),
        "window_seconds": window,
        "measured_phase_overrun_s": measured_elapsed - float(measure_s),
        "started_at": started,
        "ended_at": window_end,
        "http": http,
        "by_class": by_class,
        # Which replicas actually answered. A rung whose clients collapsed onto
        # one pod is a measurement of one pod, and nothing in the throughput
        # figure says so.
        "served_by": routing,
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
        "bottleneck": [dataclasses.asdict(v) for v in verdicts],
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
    run,
    cfg: dict,
    dataset: str,
    entries: list,
    *,
    quick: bool = False,
    replicas: int = 1,
    workers: int | None = None,
    pod_cpu_limit: float | None = None,
    pod_memory_limit_bytes: int | None = None,
    kind: str = "concurrency",
    key_prefix: str = "conc",
    refine_saturation: bool = True,
    stop_on_saturation: bool = True,
    levels: list[int] | None = None,
    repetitions: int | None = None,
    warmup_s: float | None = None,
    measure_s: float | None = None,
    generator_processes: int = 1,
) -> list[dict]:
    """Climb the concurrency ladder until saturation, then stop.

    The stop rule needs two independent signals to agree. One signal alone is
    routinely noise — a p95 can multiply on a page-cache miss, and throughput
    can plateau for one level and then climb again — and a sweep that stops on
    noise reports a ceiling that is not there.

    The default arguments are the single-replica C1 sweep; the replica-scaling
    family reuses the same ladder and stop rule per replica count with its own
    ``kind``, shorter windows, and without the long saturation re-measure
    (that ceremony exists for C1, the number everything else is a multiple of).
    """
    sweep = cfg["scenarios"]["concurrency_sweep"]
    timing = cfg["scenarios"]["timing"]
    mix = cfg["scenarios"]["query_mix"]["normal"]
    results: list[dict] = []
    baseline: dict | None = None
    previous: dict | None = None

    if levels is None:
        levels = sweep["levels"][:3] if quick else sweep["levels"]
    for concurrency in levels:
        saturated_here = False
        reps = repetitions if repetitions is not None else (1 if quick else timing["repetitions"])
        measured: list[dict] = []
        for repetition in range(reps):
            workload = load_mod.Workload(entries, mix, seed=1000 + repetition)
            key = f"{key_prefix}-{dataset}-c{concurrency}-r{repetition}"
            # The settle pause exists so a run does not inherit its
            # predecessor's state — a rung replayed from cache ran nothing, so
            # there is nothing to settle, and a resumed grid would otherwise
            # spend minutes sleeping between measurements that never happened.
            cached = run.done(key)
            ingredients = {
                "entries": entries,
                "mix": mix,
                "query_class": None,
                "seed": 1000 + repetition,
            }
            result = measure(
                run,
                cfg,
                key=key,
                kind=kind,
                dataset=dataset,
                workload=workload,
                mode="closed",
                concurrency=concurrency,
                replicas=replicas,
                workers=workers,
                pod_cpu_limit=pod_cpu_limit,
                pod_memory_limit_bytes=pod_memory_limit_bytes,
                repetition=repetition,
                warmup_s=warmup_s
                if warmup_s is not None
                else (10 if quick else (warmup_for(cfg, dataset) or timing["warmup_seconds"])),
                measure_s=measure_s
                if measure_s is not None
                else (20 if quick else timing["measure_seconds"]),
                generator_processes=generator_processes,
                workload_ingredients=ingredients,
            )
            if result:
                measured.append(result)
                results.append(result)
            if not cached:
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

        if saturated_here and not stop_on_saturation:
            # Climb anyway. The stop rule answers "has the ladder stopped
            # rising?", which is what a capacity sweep needs; a caller that has
            # to *choose* a rung needs to see the rungs above the first
            # plateau, because on short windows the plateau signal fires on
            # noise — measured: c=2 tripped it while c=4 was 10% faster.
            previous = current
            continue
        if saturated_here and not refine_saturation:
            break
        if saturated_here and not quick:
            # A saturation point earns the longer, more repeated measurement:
            # this is the number that becomes C1 and that every KEDA scenario
            # is expressed as a multiple of, so it should be the best-measured
            # figure in the suite.
            log.info("saturation at c=%d; re-measuring at length", concurrency)
            for repetition in range(timing["saturation_repetitions"]):
                workload = load_mod.Workload(entries, mix, seed=2000 + repetition)
                key = f"sat-{dataset}-c{concurrency}-r{repetition}"
                cached = run.done(key)
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
                    warmup_s=warmup_for(cfg, dataset) or timing["warmup_seconds"],
                    measure_s=timing["saturation_measure_seconds"],
                )
                if result:
                    results.append(result)
                if not cached:
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

    C1 must describe the *deployed* single replica, because the autoscaling
    families that express their rates as multiples of it run at the values
    file's defaults. So the worker sweep's off-default points are excluded:
    the limit probe (a raised ``pod_cpu_limit`` the values file does not set)
    and the extra-worker grid points (``workers`` other than the default 1).
    Both carry a non-None field precisely to be recognisable here; older
    families leave both None and are unaffected.
    """
    usable = [
        r
        for r in results
        if r.get("replicas") in (1, None)
        and r.get("pod_cpu_limit") is None
        and r.get("workers") in (None, 1)
        and _within_slo(r, slo_p95_s)
    ]
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
    results: list[dict],
    *,
    kind: str,
    replicas: int,
    workers: int | None = None,
    slo_p95_s: float,
    signals_required: int = SATURATION_SIGNALS_REQUIRED_DEFAULT,
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

    `signals_required` must be the same number the sweep climbed under
    (config/scenarios.yaml, concurrency_sweep.saturation_signals_required):
    read it from the config wherever there is a config to read. The default
    is only for callers that have none, and a copy of it here that drifted
    from the ladder's would report a bracket for a ladder that never stopped.
    """
    # `workers=None` means the caller's family does not vary the worker
    # count, not "match rows without one": the replica families predate the
    # field, and filtering them on it would empty every ladder they measured.
    at = [
        r
        for r in results
        if r.get("kind") == kind
        and r.get("replicas") == replicas
        and (workers is None or r.get("workers") == workers)
    ]
    met = [r for r in at if _within_slo(r, slo_p95_s)]
    if not met:
        return None
    best = max(met, key=lambda r: r["http"]["successful_rps"])

    def rung(r: dict) -> float:
        # open-loop rungs are offered rates; closed-loop rungs are held
        # concurrency — either way, "higher" means "more demand"
        return r.get("offered_rps") or float(r.get("concurrency") or 0.0)

    offered = rung(best)
    # A higher rung that the service failed, rather than one the generator
    # failed to offer: an invalid point brackets nothing.
    above = [
        r
        for r in at
        if rung(r) > offered and not r.get("invalid") and not _within_slo(r, slo_p95_s)
    ]
    # A closed-loop sweep brackets by construction when it saturated: the
    # plateau is a measured ceiling even though no rung failed the SLO — a
    # CPU-bound service under a closed loop degrades in latency, not errors,
    # and can sit far inside a 2 s SLO at its throughput ceiling.
    #
    # The evidence has to be the TOP rung's, not any() over the ladder. any()
    # says "some rung somewhere saturated", which is not the claim published:
    # a low rung tripping two signals under a top rung still climbing is a
    # ladder that found no ceiling, and calling it a bracket puts its ratio in
    # the efficiency headline. The two coincide today only because
    # refine_saturation=False breaks the sweep on the first saturated rung, so
    # the saturated rung IS the last one measured.
    #
    # Any point at that rung, not the top rung's first point: a rung is
    # `repetitions` measurements and concurrency_sweep() attaches the signals
    # to the median one only.
    ladder = [r for r in at if not r.get("invalid")]
    top_rung = max((rung(r) for r in ladder), default=None)
    saturated = any(
        (r.get("saturation") or {}).get("count", 0) >= signals_required
        for r in ladder
        if rung(r) == top_rung
    )
    return {
        "rps": best["http"]["successful_rps"],
        "offered_rps": offered,
        "key": best["key"],
        "bracketed": bool(above) or saturated,
        "saturated": saturated,
        "next_rung_measured": bool([r for r in at if rung(r) > offered and not r.get("invalid")]),
    }


def _pods(n: int) -> str:
    return "one replica" if n == 1 else f"{n} replicas"


def _watched_deployment(watcher_samples: list[dict], component: str) -> str:
    """The Deployment name a run's watcher actually recorded for `component`.

    The name is `<helm release>-<component>`, and the release has been renamed
    (skao-tap to egernia), so a run measured before the rename stored the old
    one. Reading it back from the artefacts is what keeps those runs
    re-analysable: a hard-coded current name would match nothing in them and
    the re-derived fleet would be silently empty rather than wrong.
    """
    for sample in watcher_samples:
        for name in sample.get("deployments") or {}:
            if name.endswith(f"-{component}"):
                return name
    return f"{cluster.RELEASE}-{component}"


def keda_timings_from_artefacts(run_dir, entry: dict, key: str, metrics_rows: list[dict]) -> dict:
    """A KEDA scenario's stage timings, re-derived from what the run stored.

    The stamps come from four sources — the scaler's series, the HPA's, the
    Pods' lifecycle and the request samples — and all four are written to the
    run directory. So a correction to how the stages are computed can reach the
    scenarios that were already measured, which is the whole reason the
    artefacts are kept.
    """
    import pathlib

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
    samples = runs_mod.read_samples(sample_path)
    return keda_analysis.timings(
        t0=entry["t_transition"],
        metrics_rows=metrics_rows,
        watcher_samples=watcher,
        pod_timings=pod_timings,
        samples=samples,
        deployment=_watched_deployment(watcher, "tap-executor"),
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
    import types

    import pyarrow.parquet as pq

    path = pathlib.Path(run_dir) / "samples" / f"{key}.parquet"
    if not path.exists():
        return {}
    table = pq.read_table(path, columns=["t_start", "t_offered"]).to_pydict()
    return stats_mod.coordinated_omission(
        [
            types.SimpleNamespace(t_start=start, t_offered=offered or 0.0)
            for start, offered in zip(table["t_start"], table["t_offered"], strict=True)
        ]
    )


def replica_capacities(
    results: list[dict],
    slo_p95_s: float,
    signals_required: int = SATURATION_SIGNALS_REQUIRED_DEFAULT,
) -> list[dict]:
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
            if r.get("kind") in ("fixed_replicas", "replica_sweep")
            and r.get("replicas") is not None
        }
    )
    out = []
    for n in counts:
        candidates = [
            (kind, found)
            for kind in ("replica_sweep", "fixed_replicas")
            if (
                found := bracketed_capacity(
                    results,
                    kind=kind,
                    replicas=n,
                    slo_p95_s=slo_p95_s,
                    signals_required=signals_required,
                )
            )
        ]
        if not candidates:
            continue
        # a bracketed capacity beats an open-ended one; among equals, the
        # higher measured throughput
        kind, found = max(candidates, key=lambda kc: (kc[1]["bracketed"], kc[1]["rps"]))
        out.append({"replicas": n, "kind": kind, **found})
    return out


def connection_arithmetic(values: dict) -> dict:
    """What a (workers, replicas) point costs the database, from the chart.

    Read from the deployed values rather than restated, for the same reason
    limits() reads them: a constant matching the values file at the time it
    was written goes wrong silently when the file moves.
    """
    pool_max = int((values.get("config") or {}).get("dbPoolMax") or 8)
    tuning = (values.get("postgresql") or {}).get("tuning") or {}
    return {
        "pool_max": pool_max,
        "max_connections": int(tuning.get("max_connections") or 0) or None,
        # The executors' own pools sit on the same server, so the API fleet's
        # ceiling has to fit around them.
        "executor_pool": pool_max * int((values.get("tapExecutor") or {}).get("replicas") or 1),
    }


def worker_capacities(
    results: list[dict],
    slo_p95_s: float,
    signals_required: int = SATURATION_SIGNALS_REQUIRED_DEFAULT,
    *,
    pool_max: int = 8,
    max_connections: int | None = None,
    executor_pool: int = 0,
) -> list[dict]:
    """Per (workers, replicas) point: the capacity, and its price at the database.

    The two axes are not interchangeable there. Every worker process holds
    its own pool, so a point's connection ceiling is
    ``replicas x workers x dbPoolMax`` — stated beside each capacity because
    the grid deliberately contains shapes (4 workers on 8 replicas at
    dbPoolMax 8 is 256 connections) that a 200-connection server cannot
    honour, and a capacity published without its arithmetic reads as a shape
    an operator could pick.

    ``executor_pool`` is the executors' own connections, which the API fleet's
    ceiling has to fit around; PostgreSQL additionally holds 3 back for
    superusers, the same accounting the chart's HPA guard applies.
    """
    points = sorted(
        {
            (r["workers"], r["replicas"])
            for r in results
            if r.get("kind") == "worker_sweep" and r.get("workers") and r.get("replicas")
        }
    )
    usable = (max_connections - 3 - executor_pool) if max_connections else None
    out = []
    for workers, replicas in points:
        found = bracketed_capacity(
            results,
            kind="worker_sweep",
            replicas=replicas,
            workers=workers,
            slo_p95_s=slo_p95_s,
            signals_required=signals_required,
        )
        if not found:
            continue
        ceiling = replicas * workers * pool_max
        out.append(
            {
                "workers": workers,
                "replicas": replicas,
                "worker_processes": replicas * workers,
                "connection_ceiling": ceiling,
                "exceeds_max_connections": bool(usable is not None and ceiling > usable),
                **found,
            }
        )
    return out


def capacity_headline(
    results: list[dict],
    slo_p95_s: float,
    signals_required: int = SATURATION_SIGNALS_REQUIRED_DEFAULT,
) -> dict:
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
        at_one = bracketed_capacity(
            results,
            kind="fixed_replicas",
            replicas=1,
            slo_p95_s=slo_p95_s,
            signals_required=signals_required,
        )
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
    for kind, label, top_n in (
        ("replica_sweep", "replica scaling efficiency at 8", 8),
        ("fixed_replicas", "replica scaling efficiency at 8", 8),
    ):
        if label in out and out[label].get("value") is not None:
            continue
        one = bracketed_capacity(
            results, kind=kind, replicas=1, slo_p95_s=slo_p95_s, signals_required=signals_required
        )
        top = bracketed_capacity(
            results,
            kind=kind,
            replicas=top_n,
            slo_p95_s=slo_p95_s,
            signals_required=signals_required,
        )
        if not (one and top):
            continue
        if one["bracketed"] and top["bracketed"] and one["rps"]:
            how = (
                "each a saturated closed-loop sweep's plateau"
                if kind == "replica_sweep"
                else "each the last rate met before a measured failure"
            )
            out[label] = {
                "value": round(top["rps"] / (top_n * one["rps"]), 3),
                "evidence": f"{top['rps']:.1f} rps on {_pods(top_n)} against "
                f"{one['rps']:.1f} on one, {how}",
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


def replica_sweep(run, cfg: dict, dataset: str, entries: list) -> list[dict]:
    """Package 14: a bounded-concurrency sweep at each replica count.

    The open-loop fixed-scaling family kept failing to bracket anything —
    seventeen of its 24 measurements were generator-capped, because offering
    a rate at or past capacity open-loop grows the in-flight count without
    bound. A closed loop cannot be capped that way, and its two-signal stop
    rule *is* the bracket: a count whose sweep saturated has a measured
    ceiling (the plateau), not just the largest rate anybody offered.

    Same ladder, same workload seeds at every count, so two counts differ in
    replicas and nothing else.
    """
    plan = cfg["scenarios"]["replica_sweep"]
    results: list[dict] = []
    cluster.set_autoscaling(api=False, executor=False)
    try:
        for replicas in plan["replicas"]:
            cluster.scale("tap-api", replicas)
            results += concurrency_sweep(
                run,
                cfg,
                dataset,
                entries,
                replicas=replicas,
                kind="replica_sweep",
                key_prefix=f"rsweep-n{replicas}",
                refine_saturation=False,
                repetitions=plan["repetitions"],
                warmup_s=plan["warmup_seconds"],
                measure_s=plan["measure_seconds"],
                generator_processes=int(plan.get("generator_processes", 1)),
            )
    finally:
        cluster.scale("tap-api", 1)
    return results


def worker_sweep(run, cfg: dict, dataset: str, entries: list) -> list[dict]:
    """Package 19: the replica ladder's sweep, once per (workers, replicas).

    The replica ladder was measured at one worker per pod against a pod whose
    CPU limit is 2, so every point in it was half-idle by construction. This
    grid varies both axes — same host, same corpus, same workload seeds, same
    stop rule — so a worker and a replica can be compared as two prices of
    the same capacity: one costs a pod, the other costs nothing but
    connections. Workers 1/2/4 against the 2-core pod is under-, at- and
    over-subscribed, which measures the chart's "set workers to the pod's CPU
    limit and no higher" rather than repeating it.

    Workers is the outer loop because changing it is a helm upgrade rolling
    every pod, where a replica change is one write to the scale subresource —
    and every upgrade resets the replica count to the values file's, so
    scale() is re-applied per point either way. No set_autoscaling() call:
    each set_workers() upgrade re-asserts the whole values file, which has
    both autoscalers off, and a flag set before the loop would not survive
    the first one.
    """
    plan = cfg["scenarios"]["worker_sweep"]
    values = cfg["chart_values"]
    pool = int((values.get("config") or {}).get("dbPoolMax") or 8)
    default_workers = int((values.get("tapApi") or {}).get("workers") or 1)
    results: list[dict] = []
    try:
        for workers in plan["workers"]:
            cluster.set_workers(workers)
            for replicas in plan["replicas"]:
                log.info(
                    "worker sweep point: workers=%d replicas=%d (pool ceiling %d connections)",
                    workers,
                    replicas,
                    workers * replicas * pool,
                )
                cluster.scale("tap-api", replicas)
                results += concurrency_sweep(
                    run,
                    cfg,
                    dataset,
                    entries,
                    replicas=replicas,
                    workers=workers,
                    kind="worker_sweep",
                    key_prefix=f"wsweep-w{workers}-n{replicas}",
                    refine_saturation=False,
                    repetitions=plan["repetitions"],
                    warmup_s=plan["warmup_seconds"],
                    measure_s=plan["measure_seconds"],
                    generator_processes=int(plan.get("generator_processes", 1)),
                )
        probe = plan.get("limit_probe")
        if probe:
            # The grid's own top worker count cannot answer the question it
            # looks like it answers: one worker costs ~1.05 cores, so at the
            # suite's 2-core limit every count past 2 measures the cgroup.
            # This point deploys the same workers against a raised limit —
            # its own kind, so worker_capacities() cannot merge it with the
            # grid point that shares its (workers, replicas).
            memory_bytes = _memory_bytes(probe.get("memory_limit"), 1 << 30)
            log.info(
                "limit probe: workers=%d replicas=%d at cpu=%s memory=%s",
                probe["workers"],
                probe["replicas"],
                probe["cpu_limit_cores"],
                probe.get("memory_limit"),
            )
            cluster.configure_api(
                workers=probe["workers"],
                cpu_limit_cores=probe["cpu_limit_cores"],
                memory_limit=probe.get("memory_limit"),
            )
            cluster.scale("tap-api", probe["replicas"])
            results += concurrency_sweep(
                run,
                cfg,
                dataset,
                entries,
                replicas=probe["replicas"],
                workers=probe["workers"],
                pod_cpu_limit=float(probe["cpu_limit_cores"]),
                pod_memory_limit_bytes=memory_bytes,
                kind="worker_limit_probe",
                key_prefix=f"wprobe-w{probe['workers']}cpu{probe['cpu_limit_cores']}"
                f"-n{probe['replicas']}",
                refine_saturation=False,
                repetitions=plan["repetitions"],
                warmup_s=plan["warmup_seconds"],
                measure_s=plan["measure_seconds"],
                generator_processes=int(plan.get("generator_processes", 1)),
            )
    finally:
        cluster.set_workers(default_workers)
        cluster.scale("tap-api", 1)
    return results


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
            generator_processes=int(
                cfg["scenarios"].get("keda_load", {}).get("generator_processes", 1)
            ),
            workload_ingredients={
                "entries": entries,
                "mix": mix,
                "query_class": None,
                "seed": 4000,
            },
        )
        if not result:
            continue

        # The transition of interest is the first step boundary where the rate
        # changes: that is T0.
        t0 = result["started_at"] + (steps[0].seconds if len(steps) > 1 else 0.0)
        watcher_samples = [
            json.loads(line)
            for line in (run.kube_dir / f"{key}-state.jsonl").read_text().splitlines()
            if line.strip()
        ]
        metrics_rows = runs_mod.read_metrics_rows(run.metrics_dir / f"{key}.parquet")
        samples = runs_mod.read_samples(run.samples_dir / f"{key}.parquet")
        timings = keda_analysis.timings(
            t0=t0,
            metrics_rows=metrics_rows,
            watcher_samples=watcher_samples,
            pod_timings=kube.pod_timings("tap-executor"),
            samples=samples,
            deployment="egernia-tap-executor",
            threshold=threshold,
            slo_p95_s=slo,
            scale_up=keda_analysis.scaling_up(step_records),
        )
        behaviour = keda_analysis.scale_behaviour(watcher_samples, "egernia-tap-executor")
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
        result["bottleneck"] = [dataclasses.asdict(v) for v in verdicts]
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
            warmup_s=warmup_for(cfg, dataset) or 15,
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
        # Hand the next family the SUITE's values back, not a hardcoded 0:
        # the chart's own default is 64 now, so a `--set limitConcurrency=0`
        # here would be this family choosing the next one's ceiling. An
        # override-free upgrade drops every `--set` the flips applied and
        # re-reads config/chart-values.yaml, which is where the suite's
        # uncapped ceiling and both disabled autoscalers actually live.
        cluster.install_chart()
        cluster.scale("tap-api", 1)
    return results


def shedding_summary(results: list[dict]) -> list[dict]:
    """The shedding ladder reduced to the numbers the package asked for:
    per held concurrency, how much of the shed load was an answer (503) and
    how much was a drop at the transport (TRANSPORT_DROP_ERRORS)."""
    rows: list[dict] = []
    for result in results:
        if result.get("kind") != "shedding":
            continue
        http = result["http"]
        errors = dict(http.get("errors_by_type") or {})
        drops = sum(int(errors.pop(name, 0)) for name in TRANSPORT_DROP_ERRORS)
        # errors_by_type repeats HTTP statuses as digit keys; the statuses
        # column already carries those, so "other" is transport errors only
        errors = {k: v for k, v in errors.items() if not str(k).isdigit()}
        statuses = {str(k): int(v) for k, v in (http.get("errors_by_status") or {}).items()}
        rows.append(
            {
                "key": result["key"],
                "replicas": result.get("replicas"),
                "held_concurrency": result.get("concurrency"),
                "requests": int(http.get("requests", 0)),
                "rps": float(http.get("rps", 0.0)),
                "refused_503": statuses.get("503", 0),
                "transport_drops": drops,
                "other_errors": {k: v for k, v in errors.items() if v},
            }
        )
    rows.sort(key=lambda r: (r["key"].split("-c")[0], r["held_concurrency"] or 0))
    return rows


# ---------------------------------------------------------------------------
# Package 18: where the API's per-request CPU goes
# ---------------------------------------------------------------------------

#: The gated set for the authorised rung. All four query operations together,
#: because the chart (and the service) refuse a partial query surface: a caller
#: refused at POST /tap/async runs the same query at /tap/sync. The metadata
#: operations come along because they are the service's own default and cost a
#: /tap/sync request nothing.
GATED_OPERATIONS = (
    "metadata.ingest",
    "metadata.amend",
    "metadata.delete",
    "jobs.create",
    "jobs.mutate",
    "jobs.delete",
    "query.sync",
)


def choose_profile_concurrency(
    results: list[dict], *, tolerance: float
) -> tuple[dict | None, float]:
    """The knee of a ladder: what to profile, and the best throughput on it.

    The lowest-concurrency rung the classifier called `TAP_CPU_BOUND` whose
    throughput is within `tolerance` of the ladder's best — `tolerance` being
    the suite's own `throughput_gain_below_fraction`, the threshold the
    saturation stop already uses for the judgement "these two throughputs are
    the same one".

    Every other candidate is wrong in a direction this ladder has actually
    demonstrated:

    * the **saturation stop** picked c=2, which tripped `throughput_plateau`
      while c=4 served 10% more;
    * the **busiest** rung picked c=8 — 95.5 rps at p95 131 ms against c=4's
      95.1 rps at p95 66 ms, so everything it added over the knee was queue,
      and a profile there attributes event-loop and threadpool work that only
      exists past it;
    * an **unsaturated** rung attributes an idle event loop.

    Returns (None, 0.0) when the ladder produced nothing measurable.
    """
    usable = [r for r in results if not r.get("invalid") and (r.get("http") or {}).get("rps")]
    cpu_bound = [
        r
        for r in usable
        if any(v["classification"] == "TAP_CPU_BOUND" for v in (r.get("bottleneck") or []))
    ]
    pool = cpu_bound or usable
    if not pool:
        return None, 0.0
    best = max(r["http"]["rps"] for r in pool)
    chosen = min(
        (r for r in pool if r["http"]["rps"] >= (1.0 - tolerance) * best),
        key=lambda r: r["concurrency"],
    )
    return chosen, best


def deployed_auth_policy(*, attempts: int = 10, delay_s: float = 3.0) -> dict:
    """What the pods now serving traffic say they enforce.

    Read from the service's own `/api/v1/auth`, not from the values file and
    not from the ConfigMap. Configuration reaches these pods through a
    ConfigMap, which a container reads once at startup — so a `helm upgrade`
    that changes only ConfigMap data is a successful upgrade that no running
    pod has read. (The chart now hashes the ConfigMap into both pod templates,
    which makes such a change a rollout; this is the check that the rollout
    happened, because the failure it guards against is silent: an
    "authenticated" rung served by pods with authentication off is a rung that
    measures the unauthenticated service twice and reports the difference as
    zero.)

    Retried, because the first request after a rollout can arrive while the
    Service still has an old endpoint in it.
    """
    import httpx

    last = ""
    for attempt in range(attempts):
        try:
            response = httpx.get(f"{BASE_URL}/api/v1/auth", timeout=10.0)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                time.sleep(delay_s)
    raise RuntimeError(f"could not read the deployed auth policy: {last}")


def require_auth_policy(*, enabled: bool, gated: tuple[str, ...] = ()) -> dict:
    """Refuse to measure a rung the pods are not configured for."""
    policy = deployed_auth_policy()
    if bool(policy.get("enabled")) != enabled:
        raise RuntimeError(
            f"the deployed pods report authentication enabled={policy.get('enabled')},"
            f" this rung needs {enabled}: the rollout did not happen"
        )
    if enabled:
        if policy.get("anonymous_tap_queries"):
            raise RuntimeError(
                "the deployed pods let anonymous callers query TAP, so /tap/sync"
                " would not verify a token and the rung would measure nothing"
            )
        deployed = tuple(sorted((policy.get("gated_operations") or {}).keys()))
        if deployed != tuple(sorted(gated)):
            raise RuntimeError(
                f"the deployed pods enforce {deployed or '()'}, this rung needs"
                f" {tuple(sorted(gated)) or '()'}"
            )
    log.info("deployed auth policy: %s", policy)
    return policy


def _profile_rung(
    run,
    cfg: dict,
    dataset: str,
    entries: list,
    *,
    key: str,
    concurrency: int,
    repetitions: int,
    warmup_s: float,
    measure_s: float,
    token: str | None = None,
    gil_only: bool | None = None,
    reference_rps: float | None = None,
) -> list[dict]:
    """One rung of the profile family: N repetitions at a fixed concurrency.

    ``gil_only`` not None makes it a profiled rung — one repetition, sampled by
    py-spy for the length of the measured window. Profiled windows are single
    repetitions on purpose: the throughput they report is only used to check
    the profiler's own overhead against the unprofiled rung beside them, and
    the samples are what the repetition would have been spent on.
    """
    settle = cfg["scenarios"]["timing"]["settle_seconds"]
    mix = cfg["scenarios"]["query_mix"]["normal"]
    results: list[dict] = []
    for repetition in range(repetitions):
        rung_key = f"{key}-r{repetition}"
        sampler = None
        if gil_only is not None:
            sampler = pyspy_mod.Pass(
                out_path=run.path / "profiles" / f"{rung_key}.folded",
                seconds=measure_s,
                rate=int(cfg["scenarios"]["profile"]["sample_rate_hz"]),
                gil_only=gil_only,
                nonblocking=bool(cfg["scenarios"]["profile"].get("nonblocking")),
            )
        result = measure(
            run,
            cfg,
            key=rung_key,
            kind="profile",
            dataset=dataset,
            workload=load_mod.Workload(entries, mix, seed=3000 + repetition),
            mode="closed",
            concurrency=concurrency,
            replicas=1,
            repetition=repetition,
            warmup_s=warmup_s,
            measure_s=measure_s,
            bearer_token=token,
            during_load=sampler,
        )
        if result:
            profile = sampler.profile if sampler is not None else None
            if profile is None and sampler is not None:
                # A replayed measurement: `measure()` returned its cached result
                # without entering the sampler, but the stacks from the pass
                # that produced it are still on disk. Re-derive from them rather
                # than report a ten-minute pass as missing.
                profile = pyspy_mod.from_folded(
                    sampler.out_path,
                    gil_only=sampler.gil_only,
                    nonblocking=sampler.nonblocking,
                    rate=sampler.rate,
                    duration_s=sampler.seconds,
                )
                if profile is not None:
                    log.info("%s: profile recovered from %s", rung_key, sampler.out_path.name)
            if profile is not None:
                result["profile"] = pyspy_mod.summarise(
                    profile,
                    requests=result["http"]["requests"],
                    window_s=result["window_seconds"],
                    cpu_cores_mean=result["resources"].get("tap_api_cpu_cores_mean", 0.0),
                )
                run.write_json(f"profiles/{rung_key}.json", result["profile"])
            elif sampler is not None:
                # Recorded rather than raised: the throughput measurement is
                # still valid, and a family that threw away a good rung
                # because the profiler missed would be worse than one that
                # says the profile is missing.
                run.invalidate(f"{rung_key}: no profile", {"detail": sampler.error})
            if profile is not None and reference_rps:
                # A profile of a perturbed worker is not a profile of the
                # service. py-spy pauses the process to walk its stacks, and on
                # a thread-heavy asyncio process that has been measured at
                # thirty times slower — which would leave a breakdown of a
                # worker nobody runs, at a concurrency it is no longer
                # saturated at. Marked rather than deleted, with the number.
                overhead = (reference_rps - (result["http"]["rps"] or 0.0)) / reference_rps
                ceiling = float(cfg["scenarios"]["profile"].get("max_overhead_fraction") or 0.10)
                result["profile"]["overhead_fraction"] = overhead
                if overhead > ceiling:
                    # Marked on the measurement as well as on the run, so the
                    # report leaves the attribution out rather than publishing
                    # a perturbed one. A missing breakdown with a stated reason
                    # and a remedy is worth more than a confident wrong split.
                    result["invalid"] = True
                    run.invalidate(
                        f"{rung_key}: the profiler cost {100 * overhead:.1f}% of throughput",
                        {
                            "detail": "re-run with nonblocking: true in"
                            " config/scenarios.yaml's profile section, or --nonblocking",
                            "reference_rps": reference_rps,
                            "profiled_rps": result["http"]["rps"],
                            "ceiling_fraction": ceiling,
                        },
                    )
            results.append(result)
        time.sleep(settle)
    return results


def profile_api_cpu(
    run,
    cfg: dict,
    dataset: str,
    entries: list,
    *,
    concurrency: int,
    with_auth: bool = True,
) -> tuple[list[dict], dict]:
    """Attribute one worker's per-request CPU, and price a bearer token.

    Six rungs at one concurrency, all at ``replicas: 1, workers: 1`` so that
    "per request" and "per worker" are the same statement:

    * ``base`` — unauthenticated, unprofiled. The reference for everything
      else, and the only rung whose throughput is comparable to the figures
      the rest of this suite publishes.
    * ``gil`` — the same load with py-spy holding to GIL-owning stacks. This
      is the ceiling's own composition: one worker is one interpreter lock.
    * ``all`` — the same load again with every non-idle thread sampled, which
      adds the work done with the lock released (libpq on the socket, the
      writers' C extensions, the threadpool handoff).
    * ``authverify`` — authentication on, nothing gated: every request now
      carries a token the service verifies against a real JWKS, and no
      authorisation decision is taken. One repetition of it is profiled, so
      verification appears as a share rather than only as a throughput delta.
    * ``authgated`` — the same, with the whole query surface enforced, so the
      marginal cost of a decision on top of verification is separable.
    * ``noauth`` — authentication off again. The auth rungs are bracketed by
      two unauthenticated ones, because they are measured after two helm
      upgrades and a pod restart, and a drift between the first rung and the
      last would otherwise be indistinguishable from the cost of a token.

    Returns the measurements and a report tying them together.
    """
    scenario = cfg["scenarios"]["profile"]
    reps = int(scenario["repetitions"])
    warmup_s = float(scenario["warmup_seconds"])
    measure_s = float(scenario["measure_seconds"])
    profile_s = float(scenario["profile_seconds"])
    results: list[dict] = []

    cluster.scale("tap-api", 1)
    policies = {"base": require_auth_policy(enabled=False)}
    base_rungs = _profile_rung(
        run,
        cfg,
        dataset,
        entries,
        key=f"prof-{dataset}-c{concurrency}-base",
        concurrency=concurrency,
        repetitions=reps,
        warmup_s=warmup_s,
        measure_s=measure_s,
    )
    results += base_rungs
    # The unprofiled throughput the profiled windows are judged against. From
    # the rung immediately before them, at the same concurrency, on the same
    # pod — not from a published figure, which would import another run's host.
    base_rps = sum(r["http"]["rps"] for r in base_rungs) / len(base_rungs) if base_rungs else None
    # No warmup on the profiled windows: the rung above just spent minutes at
    # this concurrency, so the pool, the caches and the published-table cache
    # are all warm, and a warmup here would be time py-spy is not sampling.
    results += _profile_rung(
        run,
        cfg,
        dataset,
        entries,
        key=f"prof-{dataset}-c{concurrency}-gil",
        concurrency=concurrency,
        repetitions=1,
        warmup_s=0.0,
        measure_s=profile_s,
        reference_rps=base_rps,
        gil_only=True,
    )
    results += _profile_rung(
        run,
        cfg,
        dataset,
        entries,
        key=f"prof-{dataset}-c{concurrency}-all",
        concurrency=concurrency,
        repetitions=1,
        warmup_s=0.0,
        measure_s=profile_s,
        reference_rps=base_rps,
        gil_only=False,
    )

    issuer_info: dict = {"enabled": False}
    if with_auth:
        issuer = oidc_mod.keypair()
        oidc_mod.deploy(issuer, cluster.api_image())
        token = issuer.mint()
        issuer_info = {
            "enabled": True,
            "issuer": issuer.issuer,
            "audience": issuer.audience,
            "kid": issuer.kid,
            "algorithm": "RS256",
            "key_bits": 2048,
            "jwks_cache_s": 300,
            "group": oidc_mod.GROUP,
        }
        auth_values = str(SUITE / "config/auth-values.yaml")
        try:
            cluster.install_chart({"auth.gatedOperations": "{none}"}, values_files=[auth_values])
            policies["authverify"] = require_auth_policy(enabled=True)
            verify_rungs = _profile_rung(
                run,
                cfg,
                dataset,
                entries,
                key=f"prof-{dataset}-c{concurrency}-authverify",
                concurrency=concurrency,
                repetitions=reps,
                warmup_s=warmup_s,
                measure_s=measure_s,
                token=token,
            )
            results += verify_rungs
            verify_rps = (
                sum(r["http"]["rps"] for r in verify_rungs) / len(verify_rungs)
                if verify_rungs
                else None
            )
            results += _profile_rung(
                run,
                cfg,
                dataset,
                entries,
                key=f"prof-{dataset}-c{concurrency}-authgil",
                concurrency=concurrency,
                repetitions=1,
                warmup_s=0.0,
                measure_s=profile_s,
                token=token,
                gil_only=True,
                reference_rps=verify_rps,
            )
            cluster.install_chart(
                {"auth.gatedOperations": "{" + ",".join(GATED_OPERATIONS) + "}"},
                values_files=[auth_values],
            )
            policies["authgated"] = require_auth_policy(enabled=True, gated=GATED_OPERATIONS)
            results += _profile_rung(
                run,
                cfg,
                dataset,
                entries,
                key=f"prof-{dataset}-c{concurrency}-authgated",
                concurrency=concurrency,
                repetitions=reps,
                warmup_s=warmup_s,
                measure_s=measure_s,
                token=token,
            )
        finally:
            # Back to the deployment every other family measures, whatever
            # happened above: a cluster left with authentication on would make
            # the next run's numbers a mystery.
            cluster.install_chart()
            oidc_mod.remove()
        policies["noauth"] = require_auth_policy(enabled=False)
        results += _profile_rung(
            run,
            cfg,
            dataset,
            entries,
            key=f"prof-{dataset}-c{concurrency}-noauth",
            concurrency=concurrency,
            repetitions=reps,
            warmup_s=warmup_s,
            measure_s=measure_s,
        )

    return results, profile_report(results, concurrency, issuer_info, policies)


def _rung_group(results: list[dict], suffix: str) -> list[dict]:
    """The repetitions of one rung, valid ones only.

    Matched on ``-{suffix}`` rather than on ``suffix``: the profiled
    authenticated rung is ``authgil``, and a bare suffix test would fold it
    into ``gil`` and average an authenticated profile into an
    unauthenticated one.
    """
    return [
        r
        for r in results
        if r.get("kind") == "profile"
        and r["key"].rsplit("-r", 1)[0].endswith(f"-{suffix}")
        and not r.get("invalid")
    ]


def _rung_summary(results: list[dict], suffix: str) -> dict | None:
    """Throughput, CPU and per-request CPU for one rung, over its repetitions."""
    group = _rung_group(results, suffix)
    if not group:
        return None
    rps = [r["http"]["rps"] for r in group]
    cpu = [r["resources"].get("tap_api_cpu_cores_mean", 0.0) for r in group]
    per_request_ms = [
        1000.0 * c * r["window_seconds"] / max(r["http"]["requests"], 1)
        for r, c in zip(group, cpu, strict=True)
    ]
    return {
        "rung": suffix,
        "keys": [r["key"] for r in group],
        "authenticated": bool(group[0].get("authenticated")),
        "requests": sum(r["http"]["requests"] for r in group),
        "rps": stats_mod.mean_ci(rps),
        "error_fraction": max(r["http"]["error_fraction"] for r in group),
        "p95_ms": 1000.0 * max(r["http"]["latency"]["p95_s"] for r in group),
        "api_cpu_cores": stats_mod.mean_ci(cpu),
        "cpu_ms_per_request": stats_mod.mean_ci(per_request_ms),
        # The reciprocal of throughput, for a single worker: what one request
        # occupies of the one thing there is one of.
        "worker_ms_per_request": stats_mod.mean_ci([1000.0 / v for v in rps if v]),
        "profile": next((r["profile"] for r in group if r.get("profile")), None),
    }


def profile_report(
    results: list[dict], concurrency: int, issuer: dict, policies: dict | None = None
) -> dict:
    """What the family measured, as one comparable set of rungs.

    The two things the package asks for are computed here rather than left to
    a reader: the share of a request's CPU that named frames account for, and
    the difference a verified bearer token makes to the same rung.
    """
    rungs = {
        name: _rung_summary(results, name)
        for name in ("base", "gil", "all", "authverify", "authgil", "authgated", "noauth")
    }
    report: dict = {
        "concurrency": concurrency,
        "replicas": 1,
        "workers": 1,
        "issuer": issuer,
        # What the pods said they enforced, per rung, in their own words. The
        # provenance of the authentication figures: without it, "authenticated"
        # is an assertion about a helm upgrade rather than about a deployment.
        "deployed_auth_policy": policies or {},
        "rungs": {k: v for k, v in rungs.items() if v},
    }

    base, gil, whole = rungs["base"], rungs["gil"], rungs["all"]
    if gil and gil["profile"]:
        report["attribution"] = {
            "named_fraction": gil["profile"]["named_fraction"],
            "application_fraction": gil["profile"]["application_fraction"],
            "unattributed_fraction": gil["profile"]["unattributed_fraction"],
            "by_subsystem_ms": gil["profile"]["by_subsystem_ms"],
            "top_frames": gil["profile"]["top_frames"],
            "samples": gil["profile"]["samples"],
        }
    if base and gil:
        # What the profiler cost. Stated because everything above is measured
        # while it was attached, and "the profile did not perturb the run" is
        # a claim that has to come from a number.
        base_rps = base["rps"]["mean"]
        report["profiler_overhead_fraction"] = (
            (base_rps - gil["rps"]["mean"]) / base_rps if base_rps else None
        )
    if base and whole and whole["profile"] and gil and gil["profile"]:
        # What the interpreter lock does *not* contain. Expressed as differences
        # of shares rather than as two durations: with the sampler unable to
        # achieve its requested rate, neither pass's sample count is a duration
        # (see pyspy.summarise), so the comparable quantity is how the two
        # distributions differ. A subsystem larger among on-CPU samples than
        # among GIL-holding ones is doing work with the lock released — libpq on
        # the socket, the writers' C extensions — which is CPU the single-worker
        # throughput ceiling does not contain.
        report["gil_versus_all_threads"] = {
            "cgroup_cpu_ms_per_request": base["cpu_ms_per_request"]["mean"],
            "gil_occupancy_ms_per_request": gil["profile"]["profiled_occupancy_ms_per_request"],
            "all_threads_occupancy_ms_per_request": whole["profile"][
                "profiled_occupancy_ms_per_request"
            ],
            "occupancy_unavailable_reason": gil["profile"].get("occupancy_unavailable_reason"),
            "share_off_gil": {
                name: whole["profile"]["buckets"].get(name, 0.0)
                - gil["profile"]["buckets"].get(name, 0.0)
                for name in sorted(
                    set(whole["profile"]["buckets"]) | set(gil["profile"]["buckets"])
                )
            },
        }

    unauth = [r for r in (rungs["base"], rungs["noauth"]) if r]
    if unauth and rungs["authverify"]:
        # Against the mean of the bracketing unauthenticated rungs, not against
        # the first one: two helm upgrades and a pod restart separate them.
        reference = sum(r["rps"]["mean"] for r in unauth) / len(unauth)
        reference_cpu = sum(r["cpu_ms_per_request"]["mean"] for r in unauth) / len(unauth)
        cost = {"unauthenticated_rps": reference, "unauthenticated_cpu_ms": reference_cpu}
        for name in ("authverify", "authgated"):
            rung = rungs[name]
            if not rung:
                continue
            cost[name] = {
                "rps": rung["rps"]["mean"],
                "throughput_cost_fraction": (reference - rung["rps"]["mean"]) / reference
                if reference
                else None,
                "cpu_ms_per_request": rung["cpu_ms_per_request"]["mean"],
                "cpu_ms_added_per_request": rung["cpu_ms_per_request"]["mean"] - reference_cpu,
                "error_fraction": rung["error_fraction"],
            }
        if rungs["authgil"] and rungs["authgil"]["profile"]:
            cost["verification_share_of_gil"] = rungs["authgil"]["profile"]["buckets"].get(
                "token verification", 0.0
            )
            cost["verification_ms_of_gil"] = rungs["authgil"]["profile"]["by_subsystem_ms"].get(
                "token verification", 0.0
            )
        report["authentication_cost"] = cost
    return report


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
    from egernia_core.query.adql import adql_to_postgresql

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
    import pathlib
    import shutil

    run_dir = pathlib.Path(run_dir)
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text())
    shutil.copy2(summary_path, run_dir / f"summary.superseded-{int(time.time())}.json")

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
        metrics_rows = runs_mod.read_metrics_rows(metrics_path) if metrics_path.exists() else []
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
        entry["bottleneck"] = [dataclasses.asdict(v) for v in verdicts]
        now = verdicts[0].classification if verdicts else None
        if was != now:
            changed.append((key, was, now))

    summary["bottleneck_tally"] = bottleneck.tally([e for e, _ in measurements])
    # The headline is derived from validity, so re-judging validity has to
    # re-derive it. Leaving it would keep publishing a scaling efficiency that
    # the reclassified measurements no longer support.
    entries = summary.get("runs") or []
    slo_p95_s = cfg["scenarios"]["slo"]["p95_seconds"]
    headline = summary.get("headline") or {}
    signals_required = saturation_signals_required(cfg)
    headline.update(capacity_headline(entries, slo_p95_s, signals_required))
    summary["headline"] = headline
    summary["replica_capacity"] = replica_capacities(entries, slo_p95_s, signals_required)
    summary["worker_capacity"] = worker_capacities(
        entries, slo_p95_s, signals_required, **connection_arithmetic(cfg["chart_values"])
    )
    summary["sustainable_capacity_c1"] = sustainable_capacity(entries, slo_p95_s)
    summary["reanalysed_at"] = time.time()
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str, allow_nan=False)
    )
    for key, was, now in changed:
        log.info("reclassified %s: %s -> %s", key, was, now)
    # Over everything re-judged, not just `runs` — counting the numerator over
    # both lists and the denominator over one printed "4 of 3".
    log.info("%d of %d measurements changed classification", len(changed), len(measurements))
    return summary
