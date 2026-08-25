"""Kubernetes-side collection: events, pod lifecycle timings, autoscaler state.

The autoscaling timings need better resolution than a 2-second Prometheus
scrape of kube-state-metrics can give on its own, and they need the *reason* a
decision was made, which is only in the HPA's and ScaledObject's status
conditions. So this polls the API directly for the duration of a scenario and
writes what it sees, timestamped, as JSONL — a record that can be replayed
rather than a summary that has to be trusted.

Pod lifecycle stages come from the pod's own conditions, not from wall-clock
guesses: PodScheduled, Initialized, ContainersReady and Ready are each stamped
by the cluster, and the gaps between them are what separate "the scheduler was
slow" from "the image was slow" from "the app was slow to start".
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time

log = logging.getLogger("egernia_bench.kube")

CONTEXT = "kind-egernia-bench"


def kubectl_json(*args: str) -> dict:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, *args, "-o", "json"],
        text=True,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        log.warning("kubectl %s failed: %s", " ".join(args), result.stderr.strip())
        return {}
    return json.loads(result.stdout or "{}")


def kubectl_text(*args: str) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, *args],
        text=True,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout


def events(namespaces: tuple[str, ...] = ("default", "keda")) -> list[dict]:
    """Cluster events, flattened to the fields that matter here.

    Scheduling failures, image pulls, OOM kills and scale decisions all arrive
    as events, and they are the human-readable version of every timing this
    suite computes.
    """
    collected = []
    for namespace in namespaces:
        payload = kubectl_json("get", "events", "-n", namespace)
        for item in payload.get("items", []):
            collected.append(
                {
                    "namespace": namespace,
                    "type": item.get("type"),
                    "reason": item.get("reason"),
                    "object": f"{item.get('involvedObject', {}).get('kind')}/"
                    f"{item.get('involvedObject', {}).get('name')}",
                    "message": item.get("message"),
                    "count": item.get("count"),
                    "first_seen": item.get("firstTimestamp") or item.get("eventTime"),
                    "last_seen": item.get("lastTimestamp") or item.get("eventTime"),
                }
            )
    return collected


def _stamp(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def pod_timings(component: str) -> list[dict]:
    """Per-pod lifecycle stamps, for the pod-provisioning part of a scale-out."""
    payload = kubectl_json(
        "get",
        "pods",
        "-n",
        "default",
        "-l",
        f"app.kubernetes.io/component={component}",
    )
    rows = []
    for item in payload.get("items", []):
        conditions = {
            c["type"]: _stamp(c.get("lastTransitionTime"))
            for c in item.get("status", {}).get("conditions", []) or []
        }
        containers = item.get("status", {}).get("containerStatuses", []) or []
        started = [
            _stamp((c.get("state", {}).get("running") or {}).get("startedAt")) for c in containers
        ]
        restarts = sum(c.get("restartCount", 0) for c in containers)
        terminated_reasons = [
            (c.get("lastState", {}).get("terminated") or {}).get("reason") for c in containers
        ]
        created = _stamp(item["metadata"].get("creationTimestamp"))
        row = {
            "pod": item["metadata"]["name"],
            "component": component,
            "created": created,
            "scheduled": conditions.get("PodScheduled"),
            "initialized": conditions.get("Initialized"),
            "containers_ready": conditions.get("ContainersReady"),
            "ready": conditions.get("Ready"),
            "container_started": min((s for s in started if s), default=None),
            "restarts": restarts,
            "terminated_reasons": [r for r in terminated_reasons if r],
            "node": item.get("spec", {}).get("nodeName"),
            "phase": item.get("status", {}).get("phase"),
        }
        # The gaps, which are the numbers anyone actually reads.
        if created and row["scheduled"]:
            row["schedule_latency_s"] = row["scheduled"] - created
        if row["scheduled"] and row["container_started"]:
            row["container_start_latency_s"] = row["container_started"] - row["scheduled"]
        if row["container_started"] and row["ready"]:
            row["readiness_latency_s"] = row["ready"] - row["container_started"]
        if created and row["ready"]:
            row["total_startup_s"] = row["ready"] - created
        rows.append(row)
    return rows


def autoscaler_state() -> dict:
    """One sample of everything that decides a replica count."""
    sample: dict = {"t": time.time()}
    hpas = kubectl_json("get", "hpa", "-n", "default")
    sample["hpa"] = [
        {
            "name": item["metadata"]["name"],
            "min": item["spec"].get("minReplicas"),
            "max": item["spec"].get("maxReplicas"),
            "current": item.get("status", {}).get("currentReplicas"),
            "desired": item.get("status", {}).get("desiredReplicas"),
            "last_scale_time": item.get("status", {}).get("lastScaleTime"),
            "current_metrics": item.get("status", {}).get("currentMetrics"),
            "conditions": [
                {
                    "type": c.get("type"),
                    "status": c.get("status"),
                    "reason": c.get("reason"),
                    "message": c.get("message"),
                }
                for c in item.get("status", {}).get("conditions", []) or []
            ],
        }
        for item in hpas.get("items", [])
    ]
    scaled = kubectl_json("get", "scaledobject", "-n", "default")
    sample["scaledobject"] = [
        {
            "name": item["metadata"]["name"],
            "min": item["spec"].get("minReplicaCount"),
            "max": item["spec"].get("maxReplicaCount"),
            "conditions": [
                {
                    "type": c.get("type"),
                    "status": c.get("status"),
                    "reason": c.get("reason"),
                    "message": c.get("message"),
                }
                for c in item.get("status", {}).get("conditions", []) or []
            ],
            "health": item.get("status", {}).get("health"),
        }
        for item in scaled.get("items", [])
    ]
    deployments = kubectl_json("get", "deploy", "-n", "default")
    sample["deployments"] = {
        item["metadata"]["name"]: {
            "spec_replicas": item["spec"].get("replicas"),
            "ready": item.get("status", {}).get("readyReplicas", 0),
            "available": item.get("status", {}).get("availableReplicas", 0),
            "updated": item.get("status", {}).get("updatedReplicas", 0),
        }
        for item in deployments.get("items", [])
    }
    pods = kubectl_json("get", "pods", "-n", "default")
    sample["pods"] = [
        {
            "name": item["metadata"]["name"],
            "phase": item.get("status", {}).get("phase"),
            "created": _stamp(item["metadata"].get("creationTimestamp")),
            "ready": any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in item.get("status", {}).get("conditions", []) or []
            ),
        }
        for item in pods.get("items", [])
    ]
    return sample


class StateWatcher:
    """Polls autoscaler state into a JSONL file for the life of a scenario.

    A thread rather than an async task: it shells out to kubectl, which blocks,
    and the load generator's event loop is the one thing in this process that
    must not be blocked — a stalled loop would show up as service latency.
    """

    def __init__(self, path, interval_s: float = 2.0) -> None:
        self.path = path
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples: list[dict] = []

    def __enter__(self) -> StateWatcher:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        with open(self.path, "w") as handle:
            for sample in self.samples:
                handle.write(json.dumps(sample, default=str) + "\n")

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self.samples.append(autoscaler_state())
            except Exception as exc:  # a missing CRD is not a reason to stop
                self.samples.append({"t": time.time(), "error": str(exc)})
            # Fixed cadence rather than fixed sleep: kubectl takes 50-200 ms,
            # and sleeping the full interval on top would drift the sampling
            # rate away from what the timings assume.
            self._stop.wait(max(0.0, self.interval_s - (time.time() - started)))


def replica_timeline(samples: list[dict], deployment: str) -> list[tuple[float, int, int]]:
    """(t, spec_replicas, ready) for one deployment, from watcher samples."""
    timeline = []
    for sample in samples:
        entry = (sample.get("deployments") or {}).get(deployment)
        if entry:
            timeline.append((sample["t"], entry.get("spec_replicas") or 0, entry.get("ready") or 0))
    return timeline


def config_snapshot() -> dict:
    """The exact autoscaler configuration, saved with the run.

    Required by the suite's own rules: a scale-out latency without the
    threshold it was measured against is not a result, and "the chart default"
    is not a record of what was deployed.
    """
    return {
        "scaledobject_yaml": kubectl_text("get", "scaledobject", "-n", "default", "-o", "yaml"),
        "hpa_yaml": kubectl_text("get", "hpa", "-n", "default", "-o", "yaml"),
        "deployments_yaml": kubectl_text(
            "get",
            "deploy",
            "-n",
            "default",
            "-o",
            "yaml",
            "-l",
            "app.kubernetes.io/instance=egernia",
        ),
        "keda_deployment_yaml": kubectl_text("get", "deploy", "-n", "keda", "-o", "yaml"),
    }
