"""Automatic bottleneck classification.

The point of a benchmark is the next action, and the next action depends
entirely on *which* resource ran out. The same flat throughput curve is
produced by a CPU-bound service, an I/O-bound database, an exhausted
connection pool and a saturated load generator — and the fix for each is
different and mutually useless.

So the evidence for each class is stated as a rule over measured quantities,
every class that fires is reported with the numbers that made it fire, and the
ranking is by how much of the run the condition held. LOAD_GENERATOR_BOUND is
checked first and, when it fires, everything else is reported as suspect:
nothing else can be trusted from a run where the client was the limit.
"""

from __future__ import annotations

import dataclasses
import typing

import numpy as np

CLASSES = (
    "TAP_CPU_BOUND",
    "EXECUTOR_CPU_BOUND",
    "DATABASE_CPU_BOUND",
    "DATABASE_IO_BOUND",
    "CONNECTION_POOL_BOUND",
    "MEMORY_BOUND",
    "SERIALIZATION_BOUND",
    "KEDA_SCALE_LAG",
    "LOAD_GENERATOR_BOUND",
    "UNKNOWN",
)


@dataclasses.dataclass
class Verdict:
    classification: str
    confidence: float  # fraction of the window the condition held
    evidence: dict
    explanation: str


def _series(rows: list[dict], metric: str) -> np.ndarray:
    return np.asarray([r["value"] for r in rows if r["metric"] == metric], dtype=float)


def _fraction_above(values: np.ndarray, threshold: float) -> float:
    return float((values > threshold).mean()) if values.size else 0.0


def fleet_replicas(metrics_rows: list[dict], metric: str = "api_replicas_ready") -> float:
    """Peak replicas ready during the window, never below one.

    The multiplier for every per-pod ceiling, taken from the run rather than
    from the configuration: under an autoscaler the configured count is not
    what served, and the measured one is right in both cases.
    """
    ready = _series(metrics_rows, metric)
    finite = ready[np.isfinite(ready)] if ready.size else ready
    return max(float(finite.max()), 1.0) if finite.size else 1.0


def classify(
    *,
    metrics_rows: list[dict],
    summary: dict,
    pg_summary: dict,
    recorder_cpu_peak: float,
    limits: dict,
    keda: dict | None = None,
) -> list[Verdict]:
    """Every class whose evidence is present, most-confident first."""
    verdicts: list[Verdict] = []

    api_cpu = _series(metrics_rows, "tap_api_cpu_cores")
    pg_cpu = _series(metrics_rows, "postgres_cpu_cores")
    api_throttle = _series(metrics_rows, "tap_api_throttled_seconds")
    exec_cpu = _series(metrics_rows, "tap_executor_cpu_cores")
    exec_throttle = _series(metrics_rows, "tap_executor_throttled_seconds")
    pg_throttle = _series(metrics_rows, "postgres_throttled_seconds")
    pool_in_use = _series(metrics_rows, "tap_db_connections_in_use")
    pool_wait = _series(metrics_rows, "tap_pool_wait_p95")
    api_mem = _series(metrics_rows, "tap_api_memory_bytes")
    pg_mem = _series(metrics_rows, "postgres_memory_bytes")

    # Every API series above is summed over the pods, so each ceiling compared
    # against one has to be the fleet's rather than a single pod's or a single
    # process's. Getting this wrong does not produce a missing verdict, it
    # produces a confident wrong one: an eight-replica run was called
    # CONNECTION_POOL_BOUND with seven eighths of its pool idle, and
    # MEMORY_BOUND at 220 MiB a pod against a 1 GiB-a-pod limit.
    #
    # The multiplier is the peak ready count from the run itself, not the
    # configured replicas, so an autoscaled window — where the ceiling moves
    # while the measurement runs, and the configured count is meaningless — is
    # judged against the fleet that actually served.
    serving = fleet_replicas(metrics_rows)
    api_limit = limits.get("tap_api_cpu_limit_cores", 2.0) * serving
    executors = fleet_replicas(metrics_rows, "executor_replicas_ready")
    exec_limit = limits.get("tap_executor_cpu_limit_cores", 1.0) * executors
    pg_limit = limits.get("postgres_cpu_limit_cores", 4.0)
    pool_limit = limits.get("db_pool_max_total", 8) * limits.get("tap_api_workers", 1) * serving

    # -- the client first ---------------------------------------------------
    if recorder_cpu_peak > 0.80:
        verdicts.append(
            Verdict(
                "LOAD_GENERATOR_BOUND",
                min(1.0, recorder_cpu_peak),
                {"generator_cpu_peak_fraction": recorder_cpu_peak},
                "The load generator itself peaked above 80% of the host's cores. A "
                "saturated generator produces exactly the flat throughput curve a "
                "saturated service does, so no other classification from this run "
                "can be trusted.",
            )
        )

    # -- TAP CPU ------------------------------------------------------------
    if api_cpu.size:
        hot = _fraction_above(api_cpu, 0.90 * api_limit)
        throttled = float(api_throttle.mean()) if api_throttle.size else 0.0
        if hot > 0.25 or throttled > 0.05:
            verdicts.append(
                Verdict(
                    "TAP_CPU_BOUND",
                    max(hot, min(1.0, throttled)),
                    {
                        "fraction_of_window_above_90pct_limit": hot,
                        "mean_throttled_seconds_per_second": throttled,
                        "peak_cores": float(api_cpu.max()),
                        "limit_cores": api_limit,
                    },
                    "The API's own CPU is the constraint: it sat at its ceiling, or "
                    "was CFS-throttled, for a material part of the window. The "
                    "ceiling compared against is min(pod CPU limit, workers), not "
                    "the pod limit: ADQL translation is pure-Python and holds the "
                    "GIL, so one worker cannot exceed one core whatever the cgroup "
                    "allows. Relieved by more processes (tapApi.workers) or more "
                    "pods, never by a bigger limit on one worker.",
                )
            )

    # -- executor CPU -------------------------------------------------------
    # Missing until the autoscaling family was read, and it was the resource
    # actually at its ceiling there: each executor pod sat at 0.96 of a core
    # with a two-core cgroup and no throttling, while the run was described as
    # having nothing busy. The API and the database were idle; the executors
    # were pinned.
    if exec_cpu.size:
        hot = _fraction_above(exec_cpu, 0.90 * exec_limit)
        throttled = float(exec_throttle.mean()) if exec_throttle.size else 0.0
        if hot > 0.25 or throttled > 0.05:
            verdicts.append(
                Verdict(
                    "EXECUTOR_CPU_BOUND",
                    max(hot, min(1.0, throttled)),
                    {
                        "fraction_of_window_above_90pct_limit": hot,
                        "mean_throttled_seconds_per_second": throttled,
                        "peak_cores": float(exec_cpu.max()),
                        "limit_cores": exec_limit,
                        "executor_replicas_serving": executors,
                    },
                    "The executors are at their ceiling. One executor runs one query "
                    "at a time, so a pod cannot exceed one core whatever its cgroup "
                    "allows, and the only relief is more pods. Alongside "
                    "KEDA_SCALE_LAG this is the whole story of an autoscaling "
                    "shortfall: the pods that existed were full and more were never "
                    "asked for.",
                )
            )

    # -- database CPU -------------------------------------------------------
    if pg_cpu.size:
        hot = _fraction_above(pg_cpu, 0.90 * pg_limit)
        throttled = float(pg_throttle.mean()) if pg_throttle.size else 0.0
        if hot > 0.25 or throttled > 0.05:
            verdicts.append(
                Verdict(
                    "DATABASE_CPU_BOUND",
                    max(hot, min(1.0, throttled)),
                    {
                        "fraction_of_window_above_90pct_limit": hot,
                        "mean_throttled_seconds_per_second": throttled,
                        "peak_cores": float(pg_cpu.max()),
                        "limit_cores": pg_limit,
                    },
                    "PostgreSQL is CPU-bound. Adding API replicas cannot help and will "
                    "make it worse; this needs cheaper plans, fewer rows, or a bigger "
                    "database.",
                )
            )

    # -- database I/O -------------------------------------------------------
    hit_ratio = pg_summary.get("cache_hit_ratio")
    io_read_ms = pg_summary.get("io_read_time_ms") or 0.0
    blk_read_ms = pg_summary.get("blk_read_time_ms") or 0.0
    read_time_ms = max(io_read_ms, blk_read_ms)
    window = summary.get("window_seconds") or 1.0
    # Read wait as a fraction of the wall clock, summed over backends: above
    # ~50% the database is waiting for the disk more than it is working.
    io_pressure = read_time_ms / 1000.0 / window
    if (hit_ratio is not None and hit_ratio < 0.95) or io_pressure > 0.5:
        verdicts.append(
            Verdict(
                "DATABASE_IO_BOUND",
                min(1.0, max(io_pressure, 1.0 - (hit_ratio or 1.0))),
                {
                    "cache_hit_ratio": hit_ratio,
                    "read_time_seconds": read_time_ms / 1000.0,
                    "read_wait_per_wall_second": io_pressure,
                    "blocks_read": pg_summary.get("blocks_read"),
                },
                "The working set does not fit in shared_buffers plus page cache: the "
                "database is fetching from disk. This is the class that appears as the "
                "dataset grows and is the reason the size sweep exists.",
            )
        )

    # -- pool ---------------------------------------------------------------
    if pool_in_use.size:
        saturated = _fraction_above(pool_in_use, 0.95 * pool_limit)
        waiting = float(np.nanmax(pool_wait)) if pool_wait.size else 0.0
        exhausted = _series(metrics_rows, "tap_pool_exhausted_total")
        refused = float(exhausted.max() - exhausted.min()) if exhausted.size else 0.0
        if saturated > 0.25 or waiting > 0.1 or refused > 0:
            verdicts.append(
                Verdict(
                    "CONNECTION_POOL_BOUND",
                    max(saturated, min(1.0, waiting)),
                    {
                        "fraction_of_window_pool_full": saturated,
                        "peak_pool_wait_p95_s": waiting,
                        "requests_refused_503": refused,
                        "pool_max_total": pool_limit,
                        "api_replicas_serving": serving,
                    },
                    "Requests are queueing for a database connection rather than for "
                    "the database. Raising config.dbPoolMax helps only while the "
                    "server has connections to give — otherwise this is the polite "
                    "face of DATABASE_CPU_BOUND.",
                )
            )

    # -- memory -------------------------------------------------------------
    api_mem_limit = limits.get("tap_api_memory_limit_bytes", 1 << 30) * serving
    pg_mem_limit = limits.get("postgres_memory_limit_bytes", 6 << 30)
    oom = _series(metrics_rows, "oom_events")
    if (
        (api_mem.size and _fraction_above(api_mem, 0.90 * api_mem_limit) > 0.1)
        or (pg_mem.size and _fraction_above(pg_mem, 0.90 * pg_mem_limit) > 0.1)
        or (oom.size and oom.max() > 0)
    ):
        verdicts.append(
            Verdict(
                "MEMORY_BOUND",
                1.0 if (oom.size and oom.max() > 0) else 0.5,
                {
                    "api_peak_bytes": float(api_mem.max()) if api_mem.size else None,
                    "api_limit_bytes": api_mem_limit,
                    "postgres_peak_bytes": float(pg_mem.max()) if pg_mem.size else None,
                    "oom_events": float(oom.max()) if oom.size else 0.0,
                    "api_replicas_serving": serving,
                },
                "A container approached or exceeded its memory limit. An OOM kill "
                "invalidates the run outright; sustained pressure below it still "
                "distorts latency through reclaim.",
            )
        )

    # -- serialisation ------------------------------------------------------
    # The response body dominating the time, with nothing else saturated: the
    # cost is in producing bytes, not in finding rows.
    throughput = summary.get("response_throughput_bytes_per_s") or 0.0
    mean_bytes = (summary.get("response_bytes_total") or 0.0) / max(summary.get("requests") or 1, 1)
    api_busy = _fraction_above(api_cpu, 0.60 * api_limit) if api_cpu.size else 0.0
    pg_quiet = _fraction_above(pg_cpu, 0.60 * pg_limit) < 0.1 if pg_cpu.size else False
    if mean_bytes > 200_000 and api_busy > 0.25 and pg_quiet:
        verdicts.append(
            Verdict(
                "SERIALIZATION_BOUND",
                api_busy,
                {
                    "mean_response_bytes": mean_bytes,
                    "response_throughput_bytes_per_s": throughput,
                    "api_busy_fraction": api_busy,
                },
                "Large responses with a busy API and an idle database: the time is "
                "going into formatting and streaming rows, not into finding them. "
                "This is a result-pipeline cost, not a query cost.",
            )
        )

    # -- autoscaling lag ----------------------------------------------------
    if keda:
        total = (keda.get("latencies_s") or {}).get("total_scale_out")
        recovery = (keda.get("latencies_s") or {}).get("capacity_recovery")
        if (total and total > 60) or (recovery and recovery > 120):
            verdicts.append(
                Verdict(
                    "KEDA_SCALE_LAG",
                    0.8,
                    {
                        "total_scale_out_s": total,
                        "capacity_recovery_s": recovery,
                        "stage_latencies": keda.get("latencies_s"),
                    },
                    "Capacity existed but arrived late: the errors and latency in this "
                    "window are the cost of the scaling delay rather than of a "
                    "resource ceiling. The stage breakdown says which part to attack.",
                )
            )

    if not verdicts:
        # A run that mostly failed has no bottleneck to find, and reporting it
        # as "nothing saturated" reads as a healthy result. The error rate is
        # named instead, because the failures are what has to be explained
        # before any capacity number from this run means anything.
        error_fraction = summary.get("error_fraction") or 0.0
        if error_fraction > 0.10:
            verdicts.append(
                Verdict(
                    "UNKNOWN",
                    0.0,
                    {
                        "error_fraction": error_fraction,
                        "errors_by_type": summary.get("errors_by_type"),
                        "requests": summary.get("requests"),
                    },
                    f"{100 * error_fraction:.0f}% of requests failed, so no resource "
                    "was saturated by these rules — nothing was doing enough work to "
                    "saturate anything. Explain the failures before reading any "
                    "throughput or latency figure from this run.",
                )
            )
        else:
            verdicts.append(
                Verdict(
                    "UNKNOWN",
                    0.0,
                    {"note": "no rule fired", "error_fraction": error_fraction},
                    "Nothing saturated by these rules. Either the offered load was "
                    "below every ceiling, or the limit is somewhere this suite does "
                    "not instrument — which is itself worth knowing.",
                )
            )

    # LOAD_GENERATOR_BOUND first whatever its confidence, then by confidence.
    # Sorting on confidence alone let a pinned service outrank it, which
    # contradicts the rule this module opens with: when the client was the
    # limit, the service's own numbers are not evidence about the service.
    verdicts.sort(key=lambda v: (v.classification != "LOAD_GENERATOR_BOUND", -v.confidence))
    return verdicts


def primary(verdicts: typing.Sequence[Verdict]) -> str:
    return verdicts[0].classification if verdicts else "UNKNOWN"
