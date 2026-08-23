"""Bring up, configure and interrogate the benchmark cluster.

Every step here is one the numbers depend on, which is why they are code and
not a README: the node's CPU cap, the load generator living outside it, and
the exact chart values are as much a part of a result as the request latency
is.
"""

from __future__ import annotations

import json
import logging
import pathlib
import shutil
import subprocess
import time

log = logging.getLogger("tapbench.cluster")

REPO = pathlib.Path(__file__).resolve().parents[3]
SUITE = pathlib.Path(__file__).resolve().parents[1]
CLUSTER = "tapbench"
NODE_CONTAINER = f"{CLUSTER}-control-plane"
RELEASE = "skao-tap"
KEDA_VERSION = "2.18.1"


def run(*args: str, check: bool = True, capture: bool = True, timeout: int = 900) -> str:
    log.debug("$ %s", " ".join(args))
    result = subprocess.run(
        args,
        check=False,
        text=True,
        timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({result.returncode}):\n{result.stdout}")
    return result.stdout or ""


def kubectl(*args: str, **kwargs) -> str:
    return run("kubectl", "--context", f"kind-{CLUSTER}", *args, **kwargs)


def exists() -> bool:
    return CLUSTER in run("kind", "get", "clusters", check=False).split()


def create(cpus: int, memory: str) -> None:
    """Create the cluster and impose the hardware budget on it."""
    if not exists():
        run(
            "kind",
            "create",
            "cluster",
            "--config",
            str(SUITE / "manifests/kind.yaml"),
            capture=False,
            timeout=600,
        )
    # kind has no CPU or memory setting: the node is a container, so the cap
    # goes on the container. Without this the cluster would have all 14 host
    # cores and the load generator would be competing with the service it is
    # measuring for the same silicon.
    run(
        "docker",
        "update",
        "--cpus",
        str(cpus),
        "--memory",
        memory,
        "--memory-swap",
        memory,
        NODE_CONTAINER,
    )
    log.info("node capped at %s CPUs / %s", cpus, memory)


def build_and_load_images() -> dict[str, str]:
    """Build the three images and load them into the node. Returns digests."""
    images = {
        "tapbench/tap-api:bench": ("services/tap-api/Dockerfile", "."),
        "tapbench/tap-executor:bench": ("services/tap-executor/Dockerfile", "."),
        "tapbench/tap-db:bench": ("db/Dockerfile", "db"),
    }
    digests = {}
    for tag, (dockerfile, context) in images.items():
        run(
            "docker",
            "build",
            "-t",
            tag,
            "-f",
            str(REPO / dockerfile),
            str(REPO / context),
            capture=False,
            timeout=1800,
        )
        # The image id, recorded with the run: "the same chart at a different
        # commit" is the most common reason two benchmarks disagree.
        digests[tag] = run("docker", "image", "inspect", tag, "--format", "{{.Id}}").strip()
        run("kind", "load", "docker-image", tag, "--name", CLUSTER, timeout=900)
    return digests


def install_keda() -> str:
    """Install KEDA, pinned, and return the version actually running."""
    if "keda" not in kubectl("get", "ns", "-o", "name", check=False):
        run("helm", "repo", "add", "kedacore", "https://kedacore.github.io/charts", check=False)
        run("helm", "repo", "update", "kedacore", check=False)
        run(
            "helm",
            "install",
            "keda",
            "kedacore/keda",
            "--namespace",
            "keda",
            "--create-namespace",
            "--version",
            KEDA_VERSION,
            # 5-second polling is KEDA's own interval, left as the chart sets
            # it; this only affects how often the operator publishes metrics.
            "--set",
            "prometheus.metricServer.enabled=true",
            "--set",
            "prometheus.operator.enabled=true",
            "--wait",
            "--timeout",
            "5m",
            capture=False,
            timeout=600,
        )
    return kubectl(
        "get",
        "deploy",
        "-n",
        "keda",
        "keda-operator",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].image}",
    ).strip()


def install_monitoring() -> None:
    kubectl("apply", "-f", str(SUITE / "manifests/monitoring.yaml"))
    kubectl("rollout", "status", "-n", "benchmon", "deploy/prometheus", "--timeout=180s")
    kubectl("rollout", "status", "-n", "benchmon", "deploy/kube-state-metrics", "--timeout=180s")


def install_chart(overrides: dict[str, str] | None = None) -> None:
    args = [
        "helm",
        "upgrade",
        "--install",
        RELEASE,
        str(REPO / "deploy/helm/skao-tap"),
        "--kube-context",
        f"kind-{CLUSTER}",
        "--values",
        str(SUITE / "config/chart-values.yaml"),
        "--wait",
        "--timeout",
        "10m",
    ]
    for key, value in (overrides or {}).items():
        args += ["--set", f"{key}={value}"]
    run(*args, capture=False, timeout=900)
    kubectl("apply", "-f", str(SUITE / "manifests/nodeport.yaml"))


def set_autoscaling(*, api: bool, executor: bool, api_max: int = 8, executor_max: int = 8) -> None:
    """Switch the chart's own autoscalers on or off for a scenario.

    The KEDA scenarios re-enable the repository's ScaledObject as the chart
    renders it. Its thresholds are not touched: a benchmark that retunes the
    thing it is measuring reports the tuning.
    """
    install_chart(
        {
            "horizontalAutoscaling.tapApi.enabled": str(api).lower(),
            "horizontalAutoscaling.tapApi.maxReplicas": str(api_max),
            "horizontalAutoscaling.tapExecutor.enabled": str(executor).lower(),
            "horizontalAutoscaling.tapExecutor.maxReplicas": str(executor_max),
        }
    )


def scale(component: str, replicas: int) -> None:
    """Fix a component's replica count, for the no-autoscaler runs."""
    kubectl("scale", f"deploy/{RELEASE}-{component}", f"--replicas={replicas}")
    kubectl("rollout", "status", f"deploy/{RELEASE}-{component}", "--timeout=300s")


def wait_ready(component: str, timeout_s: int = 300) -> None:
    kubectl("rollout", "status", f"deploy/{RELEASE}-{component}", f"--timeout={timeout_s}s")


def database_dsn() -> str:
    """A DSN reaching the in-cluster database from the host.

    Generation is a one-off bulk load rather than part of any measurement, so
    it goes through a port-forward; nothing timed passes through it.
    """
    return "postgresql://tap:tap@127.0.0.1:55433/tap"


def port_forward_database() -> subprocess.Popen:
    process = subprocess.Popen(
        [
            "kubectl",
            "--context",
            f"kind-{CLUSTER}",
            "port-forward",
            f"svc/{RELEASE}-postgres",
            "55433:5432",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    return process


def scaled_object_yaml() -> str:
    """The ScaledObject and HPA as the cluster actually holds them.

    Saved with every KEDA run: a scale-out timing means nothing without the
    thresholds it was measured against, and "the chart's default" is not a
    record of what was deployed.
    """
    out = []
    for kind in ("scaledobject", "hpa"):
        out.append(kubectl("get", kind, "-o", "yaml", check=False))
    return "\n---\n".join(out)


def versions() -> dict[str, str]:
    node = json.loads(kubectl("get", "node", "-o", "json"))["items"][0]
    info = node["status"]["nodeInfo"]
    postgres = kubectl(
        "exec",
        f"statefulset/{RELEASE}-postgres",
        "--",
        "psql",
        "-U",
        "tap",
        "-d",
        "tap",
        "-tAc",
        "select version()",
        check=False,
    ).strip()
    extensions = kubectl(
        "exec",
        f"statefulset/{RELEASE}-postgres",
        "--",
        "psql",
        "-U",
        "tap",
        "-d",
        "tap",
        "-tAc",
        "select extname||' '||extversion from pg_extension order by 1",
        check=False,
    ).strip()
    return {
        "kubernetes": info["kubeletVersion"],
        "container_runtime": info["containerRuntimeVersion"],
        "kernel": info["kernelVersion"],
        "os_image": info["osImage"],
        "architecture": info["architecture"],
        "node_cpu_capacity": node["status"]["capacity"]["cpu"],
        "node_memory_capacity": node["status"]["capacity"]["memory"],
        "kind": run("kind", "version").strip(),
        "helm": run("helm", "version", "--short").strip(),
        "keda_image": kubectl(
            "get",
            "deploy",
            "-n",
            "keda",
            "keda-operator",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].image}",
            check=False,
        ).strip(),
        "postgres": postgres,
        "postgres_extensions": extensions.replace("\n", ", "),
    }


def teardown() -> None:
    run("kind", "delete", "cluster", "--name", CLUSTER, check=False, capture=False)


def preflight(min_free_disk_gb: int) -> list[str]:
    """Refuse to start a run the machine cannot honestly finish."""
    problems = []
    for tool in ("docker", "kind", "kubectl", "helm"):
        if not shutil.which(tool):
            problems.append(f"{tool} is not on PATH")
    free_gb = shutil.disk_usage(str(REPO)).free / 1e9
    if free_gb < min_free_disk_gb:
        problems.append(
            f"only {free_gb:.1f} GB free, below the {min_free_disk_gb} GB floor: "
            "a database that fills the disk mid-run produces numbers about the disk"
        )
    return problems
