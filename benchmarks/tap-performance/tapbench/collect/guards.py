"""Invalid-run detection.

A benchmark's most dangerous output is a plausible number produced under
conditions nobody noticed. Each check here corresponds to a way that happens,
and every one of them *marks* the run rather than discarding it: the samples
from a run that swapped are the best evidence that it swapped.

None of these are hypothetical. A load generator that saturates its own CPU
produces a flattening throughput curve indistinguishable from a saturated
service; a Prometheus that falls behind produces a scale-out that looks
instantaneous because the intermediate samples are missing; a pod restart
mid-run resets every counter the deltas are computed from.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil

import psutil

log = logging.getLogger("tapbench.guards")


@dataclasses.dataclass
class GuardResult:
    name: str
    ok: bool
    detail: str
    measured: dict


def schedule_verdicts(
    *,
    lateness_p95_s: float | None = None,
    lateness_max_s: float | None = None,
    arrivals_dropped: int = 0,
    arrivals_issued: int = 0,
    max_arrival_lateness_s: float = 5.0,
    max_unoffered_fraction: float = 0.01,
) -> list[GuardResult]:
    """Whether the open-loop generator actually ran the experiment it claims.

    Kept out of :class:`Guards` and expressed over stored numbers rather than a
    live recorder because these two checks are the ones a finished run can be
    re-judged on: both inputs are written into every measurement, so a rule
    added after a run can still be applied to it without re-measuring.

    The two failures are opposite in the data and only one of them is visible
    in the latencies, which is why both are needed:

    * Late arrivals. The request goes out, but from an issue time the generator
      missed, so its latency includes the generator's own queue. One run
      reported 84 requests/s at a 100-second p95 while the service answered all
      29,407 requests successfully and the generator was 88 seconds behind.
    * Abandoned arrivals. At the in-flight cap the generator does not fall
      behind, it drops the arrival — lateness reads clean while most of the
      offered rate is never offered. What remains is a closed-loop measurement
      at the cap's concurrency wearing an open-loop label, which is why a sweep
      of rising rates reports the same throughput and the same p95 at every
      point above capacity: every point is the same experiment.
    """
    results: list[GuardResult] = []
    if lateness_p95_s is not None:
        results.append(
            GuardResult(
                "load_generator_kept_schedule",
                lateness_p95_s <= max_arrival_lateness_s,
                f"arrivals were {lateness_p95_s:.1f}s late at p95 (limit "
                f"{max_arrival_lateness_s:.1f}s); latencies measured from an issue "
                "time the generator missed describe the client, not the service",
                {
                    "arrival_lateness_p95_s": lateness_p95_s,
                    "arrival_lateness_max_s": lateness_max_s,
                },
            )
        )
    if arrivals_dropped:
        scheduled = arrivals_issued + arrivals_dropped
        unoffered = arrivals_dropped / max(scheduled, 1)
        results.append(
            GuardResult(
                "load_generator_offered_the_rate",
                unoffered <= max_unoffered_fraction,
                f"{100 * unoffered:.1f}% of arrivals were abandoned at the in-flight "
                f"cap (limit {100 * max_unoffered_fraction:.1f}%); {arrivals_issued} of "
                f"{scheduled} scheduled requests went out, so this is a closed-loop "
                "measurement at the cap and not the offered rate it is labelled with",
                {
                    "arrivals_dropped": arrivals_dropped,
                    "arrivals_issued": arrivals_issued,
                    "unoffered_fraction": unoffered,
                },
            )
        )
    return results


class Guards:
    """Snapshot the machine before a run, judge it after."""

    def __init__(
        self,
        min_free_disk_gb: float = 15.0,
        generator_cpu_ceiling: float = 0.80,
        swap_growth_bytes: int = 512 * 1024 * 1024,
        prometheus_coverage_floor: float = 0.5,
        max_arrival_lateness_s: float = 5.0,
        max_unoffered_fraction: float = 0.01,
    ) -> None:
        self.min_free_disk_gb = min_free_disk_gb
        self.generator_cpu_ceiling = generator_cpu_ceiling
        self.swap_growth_bytes = swap_growth_bytes
        self.prometheus_coverage_floor = prometheus_coverage_floor
        # Above this the open-loop generator was not offering the rate it
        # claims, so the latencies belong to its queue rather than to the
        # service. Five seconds is generous: it tolerates a slow start without
        # tolerating a run that measured the client.
        self.max_arrival_lateness_s = max_arrival_lateness_s
        # Arrivals the generator abandoned rather than issued late. One percent
        # tolerates the odd clash at the cap; past that the labelled offered
        # rate was never offered.
        self.max_unoffered_fraction = max_unoffered_fraction
        self.before = self._machine()

    @staticmethod
    def _machine() -> dict:
        swap = psutil.swap_memory()
        virtual = psutil.virtual_memory()
        usage = shutil.disk_usage("/")
        return {
            "swap_used_bytes": swap.used,
            "swap_in_bytes": getattr(swap, "sin", 0),
            "swap_out_bytes": getattr(swap, "sout", 0),
            "memory_available_bytes": virtual.available,
            "disk_free_bytes": usage.free,
        }

    def evaluate(
        self,
        *,
        recorder=None,
        prometheus_report: dict | None = None,
        pod_timings: list[dict] | None = None,
        metrics_rows: list[dict] | None = None,
        dropped_arrivals: int = 0,
    ) -> list[GuardResult]:
        after = self._machine()
        results: list[GuardResult] = []

        # -- the host swapped -----------------------------------------------
        # Measured on swap *written*, not swap in use: a machine that has had
        # pages parked in swap since yesterday is fine, one that is paging out
        # during the run is not.
        swapped = max(0, after["swap_out_bytes"] - self.before["swap_out_bytes"])
        results.append(
            GuardResult(
                "host_did_not_swap",
                swapped < self.swap_growth_bytes,
                f"{swapped / 2**20:.0f} MiB paged out during the run",
                {"swap_out_delta_bytes": swapped},
            )
        )

        # -- disk ------------------------------------------------------------
        free_gb = after["disk_free_bytes"] / 1e9
        results.append(
            GuardResult(
                "disk_headroom",
                free_gb >= self.min_free_disk_gb,
                f"{free_gb:.1f} GB free at the end of the run (floor {self.min_free_disk_gb} GB)",
                {"free_gb": free_gb},
            )
        )

        # -- the load generator itself ---------------------------------------
        if recorder is not None:
            peak = recorder.generator_cpu_peak
            results.append(
                GuardResult(
                    "load_generator_had_headroom",
                    peak < self.generator_cpu_ceiling,
                    f"generator's busiest process peaked at {100 * peak:.0f}% of one "
                    f"core (its whole budget: one asyncio loop per process); above "
                    f"{100 * self.generator_cpu_ceiling:.0f}% its own limit becomes "
                    "indistinguishable from the service's",
                    {"generator_cpu_peak": peak},
                )
            )

        # -- the generator honoured its own arrival schedule -----------------
        lateness_p95 = lateness_max = None
        if recorder is not None:
            lateness = sorted(s.t_start - s.t_offered for s in recorder.samples if s.t_offered > 0)
            if lateness:
                lateness_p95 = lateness[int(0.95 * (len(lateness) - 1))]
                lateness_max = lateness[-1]
        results += schedule_verdicts(
            lateness_p95_s=lateness_p95,
            lateness_max_s=lateness_max,
            arrivals_dropped=dropped_arrivals,
            arrivals_issued=len(recorder.samples) if recorder is not None else 0,
            max_arrival_lateness_s=self.max_arrival_lateness_s,
            max_unoffered_fraction=self.max_unoffered_fraction,
        )

        # -- monitoring completeness -----------------------------------------
        if prometheus_report is not None:
            thin = prometheus_report.get("metrics_below_half_coverage", [])
            # Metrics with no data at all are reported separately: on a
            # non-KEDA run the keda_* series are legitimately absent, and
            # calling that a lost sample would cry wolf on every run.
            results.append(
                GuardResult(
                    "prometheus_coverage",
                    not thin,
                    f"{len(thin)} series returned under half their expected points"
                    + (f": {', '.join(thin[:6])}" if thin else ""),
                    {
                        "thin_series": thin,
                        "empty_series": prometheus_report.get("metrics_with_no_data", []),
                    },
                )
            )

        # -- nothing restarted or was killed ---------------------------------
        if pod_timings is not None:
            restarted = [p["pod"] for p in pod_timings if p.get("restarts")]
            oom = [
                p["pod"] for p in pod_timings if "OOMKilled" in (p.get("terminated_reasons") or [])
            ]
            results.append(
                GuardResult(
                    "no_unexpected_restarts",
                    not restarted,
                    f"restarted: {', '.join(restarted)}" if restarted else "no container restarted",
                    {"restarted": restarted},
                )
            )
            results.append(
                GuardResult(
                    "no_oom_kills",
                    not oom,
                    f"OOM-killed: {', '.join(oom)}" if oom else "no OOM kills",
                    {"oom_killed": oom},
                )
            )

        if metrics_rows:
            # The cluster's own view of the same two questions, which catches
            # a container that restarted and recovered before the pod list was
            # read.
            oom_events = max(
                (r["value"] for r in metrics_rows if r["metric"] == "oom_events"),
                default=0.0,
            )
            results.append(
                GuardResult(
                    "no_oom_events_observed",
                    oom_events == 0.0,
                    f"cAdvisor reported OOM events at rate {oom_events:.3f}/s",
                    {"peak_oom_event_rate": oom_events},
                )
            )

        for result in results:
            (log.info if result.ok else log.error)(
                "guard %-32s %s — %s",
                result.name,
                "ok" if result.ok else "FAILED",
                result.detail,
            )
        return results


def apply(run, results: list[GuardResult]) -> bool:
    """Record the guards; mark the run invalid for any that failed."""
    run.write_json("guards.json", [dataclasses.asdict(r) for r in results])
    failures = [r for r in results if not r.ok]
    for failure in failures:
        run.invalidate(failure.name, {"detail": failure.detail, **failure.measured})
    return not failures
