"""Prometheus collection.

Every series this suite needs, queried back over the run's own window at the
scrape resolution and written to Parquet. Read back after the fact rather than
polled live: the numbers then come from the same store the cluster's own
alerting would use, and a collector that fell behind cannot silently drop a
window it never saw.

The queries are named and grouped because the report is organised by question,
not by metric name.
"""

from __future__ import annotations

import logging
import time
import typing

import httpx

log = logging.getLogger("tapbench.prometheus")

STEP_SECONDS = 2

# Pod selectors, one per component. Spelled out rather than derived from each
# other: a selector built by string-replacing one component's name into
# another's is one rename away from silently measuring the wrong pods, and a
# metric attributed to the wrong container is worse than a missing one.
API = 'namespace="default",pod=~"skao-tap-tap-api-.*",container!=""'
EXEC = 'namespace="default",pod=~"skao-tap-tap-executor-.*",container!=""'
PG = 'namespace="default",pod=~"skao-tap-postgres-.*",container!=""'
# Network and filesystem series are per pod, not per container, so they need
# selectors without the container filter.
API_POD = 'namespace="default",pod=~"skao-tap-tap-api-.*"'
PG_POD = 'namespace="default",pod=~"skao-tap-postgres-.*"'

# Small builders rather than one long f-string per metric: the expressions are
# then short enough to read at a glance, and a selector cannot drift from the
# component it is meant to name.


def _rate(metric: str, selector: str, window: str = "15s") -> str:
    return f"sum(rate({metric}{{{selector}}}[{window}]))"


def _gauge(metric: str, selector: str) -> str:
    return f"sum({metric}{{{selector}}})"


def _deploy(metric: str, component: str) -> str:
    return f'{metric}{{namespace="default",deployment="skao-tap-{component}"}}'


def _hpa(metric: str) -> str:
    return f'{metric}{{namespace="default"}}'


def _quantile(bucket: str, extra: str = "") -> str:
    inner = f"{bucket}{{{extra}}}" if extra else bucket
    return f"histogram_quantile(0.95, sum by (le) (rate({inner}[30s])))"


CPU = "container_cpu_usage_seconds_total"
THROTTLE = "container_cpu_cfs_throttled_seconds_total"
WORKING_SET = "container_memory_working_set_bytes"

QUERIES: dict[str, str] = {
    # -- CPU. rate() over 15s: shorter is mostly quantisation noise on a 2s
    # scrape, longer smooths away the spike a scale-out is supposed to show.
    "tap_api_cpu_cores": _rate(CPU, API),
    "tap_executor_cpu_cores": _rate(CPU, EXEC),
    "postgres_cpu_cores": _rate(CPU, PG),
    "tap_api_cpu_cores_per_pod": f"sum by (pod) (rate({CPU}{{{API}}}[15s]))",
    # -- Throttling: a pod at its limit looks idle in "cores used" and slow
    # everywhere else. This is how that shows up.
    "tap_api_throttled_seconds": _rate(THROTTLE, API),
    "tap_executor_throttled_seconds": _rate(THROTTLE, EXEC),
    "postgres_throttled_seconds": _rate(THROTTLE, PG),
    # -- Memory
    "tap_api_memory_bytes": _gauge(WORKING_SET, API),
    "tap_executor_memory_bytes": _gauge(WORKING_SET, EXEC),
    "postgres_memory_bytes": _gauge(WORKING_SET, PG),
    "tap_api_rss_bytes": _gauge("container_memory_rss", API),
    "postgres_rss_bytes": _gauge("container_memory_rss", PG),
    # -- Liveness: these invalidate a run rather than decorate it.
    "oom_events": "sum(rate(container_oom_events_total[1m])) or vector(0)",
    "container_restarts": (
        'sum(kube_pod_container_status_restarts_total{namespace="default"}) or vector(0)'
    ),
    "pods_not_ready": (
        'count(kube_pod_status_ready{namespace="default",condition="false"} == 1) or vector(0)'
    ),
    # -- Network and disk
    "tap_api_net_rx_bytes": _rate("container_network_receive_bytes_total", API_POD),
    "tap_api_net_tx_bytes": _rate("container_network_transmit_bytes_total", API_POD),
    "postgres_fs_read_bytes": _rate("container_fs_reads_bytes_total", PG_POD),
    "postgres_fs_write_bytes": _rate("container_fs_writes_bytes_total", PG_POD),
    # -- Replica counts: the three that differ from each other during a
    # scale-out, and the whole subject of the KEDA timings.
    "api_replicas_desired": _deploy("kube_deployment_spec_replicas", "tap-api"),
    "api_replicas_ready": _deploy("kube_deployment_status_replicas_ready", "tap-api"),
    "api_replicas_available": _deploy("kube_deployment_status_replicas_available", "tap-api"),
    "executor_replicas_desired": _deploy("kube_deployment_spec_replicas", "tap-executor"),
    "executor_replicas_ready": _deploy("kube_deployment_status_replicas_ready", "tap-executor"),
    "executor_replicas_available": _deploy(
        "kube_deployment_status_replicas_available", "tap-executor"
    ),
    # -- HPA, including the one KEDA creates for itself
    "hpa_current_replicas": _hpa("kube_horizontalpodautoscaler_status_current_replicas"),
    "hpa_desired_replicas": _hpa("kube_horizontalpodautoscaler_status_desired_replicas"),
    "hpa_min_replicas": _hpa("kube_horizontalpodautoscaler_spec_min_replicas"),
    "hpa_max_replicas": _hpa("kube_horizontalpodautoscaler_spec_max_replicas"),
    "hpa_target_metric": _hpa("kube_horizontalpodautoscaler_spec_target_metric"),
    # -- KEDA's own view. keda_scaler_metrics_value is the number the decision
    # is made on, so T1 (threshold crossing) is read from it.
    "keda_scaler_active": "keda_scaler_active",
    "keda_scaler_metrics_value": "keda_scaler_metrics_value",
    "keda_scaler_metrics_latency_seconds": (
        "keda_scaler_metrics_latency_seconds or keda_scaler_metrics_latency"
    ),
    "keda_scaler_errors": "sum(rate(keda_scaler_detail_errors_total[1m])) or vector(0)",
    "keda_scaled_object_errors": ("sum(rate(keda_scaled_object_errors_total[1m])) or vector(0)"),
    # -- The service's own signals, which is what package 8 exported them for
    "tap_oldest_queued_job_seconds": "max(tap_oldest_queued_job_seconds)",
    "tap_jobs_queued": 'max(tap_jobs{phase="QUEUED"})',
    "tap_jobs_executing": 'max(tap_jobs{phase="EXECUTING"})',
    "tap_db_connections_in_use": "sum(tap_db_connections_in_use)",
    "tap_pool_exhausted_total": "sum(tap_db_pool_exhausted_total)",
    "tap_pool_wait_p95": _quantile("tap_db_pool_wait_seconds_bucket"),
    "tap_query_duration_p95_sync": _quantile("tap_query_duration_seconds_bucket", 'kind="sync"'),
    "tap_query_duration_p95_async": _quantile("tap_query_duration_seconds_bucket", 'kind="async"'),
    "tap_jobs_completed_total": "sum(tap_jobs_completed_total)",
    # -- The node itself: the question container metrics cannot answer, which
    # is whether the machine ran out rather than the pod.
    "node_cpu_cores": f'sum(rate({CPU}{{id="/"}}[15s]))',
    "node_memory_working_set_bytes": f'{WORKING_SET}{{id="/"}}',
}


class Prometheus:
    def __init__(self, base_url: str = "http://127.0.0.1:30090") -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=60.0)

    def close(self) -> None:
        self.client.close()

    def ready(self, timeout_s: float = 120.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if self.client.get(f"{self.base_url}/-/ready").status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(2)
        return False

    def instant(self, query: str) -> list[dict]:
        response = self.client.get(f"{self.base_url}/api/v1/query", params={"query": query})
        response.raise_for_status()
        return response.json()["data"]["result"]

    def range(self, query: str, start: float, end: float, step: int = STEP_SECONDS) -> list[dict]:
        response = self.client.get(
            f"{self.base_url}/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "step": step},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            log.warning("prometheus rejected %s: %s", query, payload.get("error"))
            return []
        return payload["data"]["result"]

    def collect(
        self, start: float, end: float, step: int = STEP_SECONDS
    ) -> tuple[list[dict], dict]:
        """Every named query over the window, plus a completeness report.

        The completeness report is the point of returning a tuple: a series
        that came back with a third of its expected points means the run is
        describing a Prometheus that fell behind, and that has to be visible
        rather than smoothed over by the plotting.
        """
        rows: list[dict] = []
        expected = max(1, int((end - start) / step))
        coverage: dict[str, float] = {}
        for name, query in QUERIES.items():
            try:
                results = self.range(query, start, end, step)
            except httpx.HTTPError as exc:
                log.warning("query %s failed: %s", name, exc)
                coverage[name] = 0.0
                continue
            points = 0
            for series in results:
                labels = series.get("metric", {})
                label_text = ",".join(
                    f"{k}={v}" for k, v in sorted(labels.items()) if k != "__name__"
                )
                for timestamp, value in series.get("values", []):
                    points += 1
                    try:
                        numeric = float(value)
                    except ValueError:
                        continue
                    rows.append(
                        {
                            "metric": name,
                            "labels": label_text,
                            "t": float(timestamp),
                            "value": numeric,
                        }
                    )
            # Several series per metric is normal (per pod, per scaler), so
            # coverage is measured against the busiest single series.
            per_series = max((len(s.get("values", [])) for s in results), default=0)
            coverage[name] = per_series / expected
        report = {
            "expected_points_per_series": expected,
            "coverage": coverage,
            "metrics_with_no_data": sorted(k for k, v in coverage.items() if v == 0.0),
            "metrics_below_half_coverage": sorted(k for k, v in coverage.items() if 0.0 < v < 0.5),
        }
        return rows, report

    def write(self, rows: list[dict], path) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        if not rows:
            log.warning("no prometheus rows to write to %s", path)
            return
        columns: dict[str, list] = {k: [] for k in ("metric", "labels", "t", "value")}
        for row in rows:
            for key in columns:
                columns[key].append(row[key])
        pq.write_table(pa.table(columns), path, compression="zstd")

    def series(
        self, rows: list[dict], metric: str, label_filter: str | None = None
    ) -> list[tuple[float, float]]:
        """One metric as an ordered (t, value) list, for the timing maths."""
        points = [
            (r["t"], r["value"])
            for r in rows
            if r["metric"] == metric and (label_filter is None or label_filter in r["labels"])
        ]
        return sorted(points)


def first_crossing(
    points: typing.Sequence[tuple[float, float]], threshold: float, *, above: bool = True
) -> float | None:
    """When a series first crosses a threshold. None if it never does."""
    for timestamp, value in points:
        if (value > threshold) if above else (value < threshold):
            return timestamp
    return None


def first_change(points: typing.Sequence[tuple[float, float]]) -> float | None:
    """When a step series first changes value."""
    if not points:
        return None
    initial = points[0][1]
    for timestamp, value in points:
        if value != initial:
            return timestamp
    return None
