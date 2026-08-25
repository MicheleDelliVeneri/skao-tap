"""Turning samples into statistics, with the uncertainty attached.

Two kinds of interval appear here and they answer different questions:

* Across repetitions, the mean of N runs gets a Student-t interval. With three
  or five repetitions the normal approximation is visibly wrong, and the whole
  reason for repeating a measurement is to say how much it moves.
* Within one run, a percentile gets a bootstrap interval. There is no clean
  closed form for the uncertainty of a p99, and resampling needs no assumption
  about the shape of a latency distribution — which is never normal.

A number without an interval invites a conclusion it cannot support: two
throughputs differing by 3% with 8% run-to-run spread are the same throughput.
"""

from __future__ import annotations

import math
import typing

import numpy as np

PERCENTILES = (50, 75, 90, 95, 99, 99.9)

# Two-sided 95% Student-t critical values by degrees of freedom. A table
# rather than scipy: this is the only distribution function the suite needs,
# and it is not worth a dependency that has to be built from source on some
# machines.
T95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


def t_critical(df: int) -> float:
    if df <= 0:
        return math.inf
    if df in T95:
        return T95[df]
    return 1.96  # beyond 30 the difference stops mattering


def mean_ci(values: typing.Sequence[float]) -> dict:
    """Mean of repeated measurements with a 95% Student-t interval."""
    array = np.asarray([v for v in values if v is not None], dtype=float)
    n = array.size
    if n == 0:
        return {"n": 0, "mean": None, "ci95_low": None, "ci95_high": None, "stddev": None}
    mean = float(array.mean())
    if n == 1:
        # One measurement has no spread to report. Said explicitly rather than
        # reported as +/- 0, which would read as perfect precision.
        return {"n": 1, "mean": mean, "ci95_low": None, "ci95_high": None, "stddev": None}
    stddev = float(array.std(ddof=1))
    half = t_critical(n - 1) * stddev / math.sqrt(n)
    return {
        "n": n,
        "mean": mean,
        "stddev": stddev,
        "ci95_low": mean - half,
        "ci95_high": mean + half,
    }


def bootstrap_ci(
    values: np.ndarray, statistic, *, resamples: int = 2000, seed: int = 12345
) -> tuple[float | None, float | None]:
    """A 95% percentile-bootstrap interval for any statistic.

    Seeded, so the interval is reproducible: an interval that moves between
    two analyses of the same samples is not evidence of anything.
    """
    if values.size < 20:
        return None, None
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, values.size, size=(resamples, values.size))
    estimates = statistic(values[indices], axis=1)
    return float(np.percentile(estimates, 2.5)), float(np.percentile(estimates, 97.5))


def served_by(samples) -> dict:
    """Which replica answered, and how unevenly the load was spread.

    A closed-loop client holds keep-alive connections, so kube-proxy picks a
    pod per *connection* and the choice persists for the window. At low
    concurrency against several replicas that is a coin flip: two clients that
    land on the same pod measure one pod's throughput, and the rung reads half
    what the fleet can do. It is real (a two-client rung has been seen to
    report 177.6 rps and 98.1 rps on two repetitions of identical work) and it
    is invisible in a throughput number.

    ``concentration`` is the busiest pod's share of the requests. Against N
    ready replicas, even spreading puts it near 1/N and a rung where clients
    collapsed onto one pod puts it near 1 — so a reader can tell the two apart
    without re-running anything. Empty when the service did not say, which is
    every run before the generator started reading the right header.
    """
    counts: dict[str, int] = {}
    for sample in samples:
        if sample.pod:
            counts[sample.pod] = counts.get(sample.pod, 0) + 1
    total = sum(counts.values())
    if not total:
        return {"pods": {}, "distinct_pods": 0, "concentration": None}
    return {
        "pods": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "distinct_pods": len(counts),
        "concentration": max(counts.values()) / total,
    }


def summarise(samples, window_seconds: float, *, with_ci: bool = True) -> dict:
    """Every HTTP figure the suite reports, from one run's samples."""
    total = len(samples)
    if total == 0:
        return {"requests": 0, "window_seconds": window_seconds}

    latencies = np.asarray([s.latency_s for s in samples], dtype=float)
    ttfbs = np.asarray([s.ttfb_s for s in samples], dtype=float)
    byte_counts = np.asarray([s.response_bytes for s in samples], dtype=float)
    ok = np.asarray([200 <= s.status < 300 and not s.error for s in samples])

    successes = int(ok.sum())
    errors_by_status: dict[str, int] = {}
    errors_by_type: dict[str, int] = {}
    timeouts = 0
    for sample in samples:
        if 200 <= sample.status < 300 and not sample.error:
            continue
        key = sample.error or str(sample.status)
        errors_by_type[key] = errors_by_type.get(key, 0) + 1
        errors_by_status[str(sample.status)] = errors_by_status.get(str(sample.status), 0) + 1
        if "Timeout" in sample.error or sample.status == 504:
            timeouts += 1

    successful_latencies = latencies[ok] if successes else np.asarray([])
    mean = float(latencies.mean())
    stddev = float(latencies.std(ddof=1)) if total > 1 else 0.0

    summary = {
        "requests": total,
        "successful": successes,
        "window_seconds": window_seconds,
        "rps": total / window_seconds if window_seconds else None,
        "successful_rps": successes / window_seconds if window_seconds else None,
        "error_count": total - successes,
        "error_fraction": (total - successes) / total,
        "timeout_count": timeouts,
        "timeout_fraction": timeouts / total,
        "errors_by_type": errors_by_type,
        "errors_by_status": errors_by_status,
        "response_bytes_total": float(byte_counts.sum()),
        # What one response weighed. The total divided by the window says how
        # fast bytes left the service; this says how many of them a client had
        # to receive to get its answer, which is the other half of what a
        # result format costs.
        "mean_response_bytes": float(byte_counts.mean()),
        "response_throughput_bytes_per_s": float(byte_counts.sum()) / window_seconds
        if window_seconds
        else None,
        "latency": {
            "mean_s": mean,
            "stddev_s": stddev,
            # Coefficient of variation: the number that says whether a mean is
            # worth quoting at all.
            "coefficient_of_variation": stddev / mean if mean else None,
            "min_s": float(latencies.min()),
            "max_s": float(latencies.max()),
            **{
                f"p{str(p).replace('.', '_')}_s": float(np.percentile(latencies, p))
                for p in PERCENTILES
            },
        },
        "ttfb": {
            "mean_s": float(ttfbs.mean()),
            **{
                f"p{str(p).replace('.', '_')}_s": float(np.percentile(ttfbs, p))
                for p in PERCENTILES
            },
        },
    }
    # Successful-only percentiles alongside the all-request ones: a run where
    # 5% of requests fail fast has a flattered p95 if failures are counted and
    # a flattered error story if they are not, so both are reported.
    if successes:
        summary["latency_successful"] = {
            "mean_s": float(successful_latencies.mean()),
            **{
                f"p{str(p).replace('.', '_')}_s": float(np.percentile(successful_latencies, p))
                for p in PERCENTILES
            },
        }
    if with_ci:
        low, high = bootstrap_ci(latencies, lambda a, axis: np.mean(a, axis=axis))
        summary["latency"]["mean_ci95"] = [low, high]
        low, high = bootstrap_ci(latencies, lambda a, axis: np.percentile(a, 95, axis=axis))
        summary["latency"]["p95_ci95"] = [low, high]
        low, high = bootstrap_ci(latencies, lambda a, axis: np.percentile(a, 99, axis=axis))
        summary["latency"]["p99_ci95"] = [low, high]
    return summary


def by_query_class(samples, window_seconds: float) -> dict[str, dict]:
    """The same summary per class, which is where the interesting spread is."""
    grouped: dict[str, list] = {}
    for sample in samples:
        grouped.setdefault(sample.query_class, []).append(sample)
    return {
        cls: summarise(rows, window_seconds, with_ci=False) for cls, rows in sorted(grouped.items())
    }


def coordinated_omission(samples) -> dict:
    """How far behind its own schedule the generator fell.

    Open-loop only. If the generator could not issue requests when it meant
    to, the latencies it recorded are measured from a start time the service
    was not responsible for, and the offered rate in the report is a rate that
    was never actually offered.
    """
    lateness = np.asarray(
        [s.t_start - s.t_offered for s in samples if s.t_offered > 0], dtype=float
    )
    if lateness.size == 0:
        return {"samples": 0}
    return {
        "samples": int(lateness.size),
        "mean_lateness_s": float(lateness.mean()),
        "p95_lateness_s": float(np.percentile(lateness, 95)),
        "max_lateness_s": float(lateness.max()),
        "fraction_late_over_100ms": float((lateness > 0.1).mean()),
    }


def saturation_signals(
    current: dict, baseline: dict, previous: dict | None, resources: dict, thresholds: dict
) -> dict:
    """Which saturation signals this concurrency level trips.

    Deliberately a set rather than a verdict: one signal is noise — a p95 can
    double for a page-cache miss — and the sweep only stops when several agree,
    which is what a real ceiling looks like.
    """
    signals: dict[str, bool] = {}
    detail: dict[str, float | None] = {}

    if previous and previous.get("rps"):
        gain = (current["rps"] - previous["rps"]) / previous["rps"]
        signals["throughput_plateau"] = gain < thresholds["throughput_gain_below_fraction"]
        detail["throughput_gain"] = gain

    base_p95 = (baseline.get("latency") or {}).get("p95_s")
    if base_p95:
        ratio = current["latency"]["p95_s"] / base_p95
        signals["latency_blown"] = ratio > thresholds["p95_multiple_of_baseline"]
        detail["p95_multiple_of_baseline"] = ratio

    signals["errors"] = current["error_fraction"] > thresholds["error_rate_above_fraction"]
    detail["error_fraction"] = current["error_fraction"]

    tap_cpu = resources.get("tap_api_cpu_fraction_of_limit")
    if tap_cpu is not None:
        signals["tap_cpu_saturated"] = tap_cpu > thresholds["tap_cpu_above_fraction"]
        detail["tap_api_cpu_fraction_of_limit"] = tap_cpu
    pg_cpu = resources.get("postgres_cpu_fraction_of_limit")
    if pg_cpu is not None:
        signals["postgres_cpu_saturated"] = pg_cpu > thresholds["postgres_cpu_above_fraction"]
        detail["postgres_cpu_fraction_of_limit"] = pg_cpu

    tripped = sorted(name for name, value in signals.items() if value)
    return {"signals": signals, "detail": detail, "tripped": tripped, "count": len(tripped)}
