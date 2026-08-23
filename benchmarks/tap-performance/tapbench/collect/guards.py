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


class Guards:
    """Snapshot the machine before a run, judge it after."""

    def __init__(
        self,
        min_free_disk_gb: float = 15.0,
        generator_cpu_ceiling: float = 0.80,
        swap_growth_bytes: int = 512 * 1024 * 1024,
        prometheus_coverage_floor: float = 0.5,
    ) -> None:
        self.min_free_disk_gb = min_free_disk_gb
        self.generator_cpu_ceiling = generator_cpu_ceiling
        self.swap_growth_bytes = swap_growth_bytes
        self.prometheus_coverage_floor = prometheus_coverage_floor
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
                    f"generator peaked at {100 * peak:.0f}% of the host's cores; above "
                    f"{100 * self.generator_cpu_ceiling:.0f}% its own limit becomes "
                    "indistinguishable from the service's",
                    {"generator_cpu_peak": peak},
                )
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
