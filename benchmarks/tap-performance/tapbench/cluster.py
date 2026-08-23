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


IMAGES = {
    "tap-api": ("services/tap-api/Dockerfile", "."),
    "tap-executor": ("services/tap-executor/Dockerfile", "."),
    "tap-db": ("db/Dockerfile", "db"),
}

#: The tag the cluster is currently running, so every later `helm upgrade`
#: keeps deploying the images this run built.
_image_tag: str | None = None


def build_and_load_images() -> tuple[str, dict[str, str]]:
    """Build the three images under a content-addressed tag and load them.

    The tag is derived from the built image ids rather than being a fixed
    ``:bench``, and that is not cosmetic. Rebuilding a mutable tag and running
    ``kind load`` leaves the pod spec byte-identical, so Kubernetes has nothing
    to roll out and the running pods keep serving the *previous* image — a
    benchmark that rebuilds its images and then measures the old code, with no
    symptom except numbers that do not move. A content-addressed tag makes new
    code a different pod spec, so the rollout is forced and observable.
    """
    global _image_tag
    import hashlib

    digests: dict[str, str] = {}
    for name, (dockerfile, context) in IMAGES.items():
        staging = f"tapbench/{name}:staging"
        run(
            "docker",
            "build",
            "-t",
            staging,
            "-f",
            str(REPO / dockerfile),
            str(REPO / context),
            capture=False,
            timeout=1800,
        )
        digests[name] = run("docker", "image", "inspect", staging, "--format", "{{.Id}}").strip()
    tag = (
        "bench-"
        + hashlib.sha256("".join(digests[name] for name in sorted(digests)).encode()).hexdigest()[
            :12
        ]
    )
    for name in IMAGES:
        run("docker", "tag", f"tapbench/{name}:staging", f"tapbench/{name}:{tag}")
        run(
            "kind", "load", "docker-image", f"tapbench/{name}:{tag}", "--name", CLUSTER, timeout=900
        )
    _image_tag = tag
    log.info("images built and loaded as %s", tag)
    return tag, {f"tapbench/{name}:{tag}": digest for name, digest in digests.items()}


def deployed_image_tag() -> str | None:
    """The tag the release is already using, for a run that skips the build."""
    values = run(
        "helm",
        "get",
        "values",
        RELEASE,
        "--kube-context",
        f"kind-{CLUSTER}",
        "-o",
        "json",
        check=False,
    )
    try:
        return json.loads(values).get("image", {}).get("tag")
    except Exception:
        return None


def verify_running_images(expected_tag: str) -> None:
    """Refuse to measure pods that are not running the images just built.

    The check is on the pod spec rather than on an image id, because kind
    rewrites ids on import — the id in a pod's status has no relation to the
    one docker built. The tag is what the chart set and what the kubelet
    resolved, so it is the thing that can actually be compared.
    """
    running = kubectl(
        "get",
        "pods",
        "-l",
        "app.kubernetes.io/instance=" + RELEASE,
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}={.spec.containers[*].image}{'\n'}{end}",
    )
    wrong = [line for line in running.splitlines() if line.strip() and expected_tag not in line]
    if wrong:
        raise RuntimeError(
            "these pods are not running the images this run built "
            f"(expected tag {expected_tag}):\n  " + "\n  ".join(wrong)
        )
    log.info("all pods confirmed running %s", expected_tag)


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
    """Deploy the chart, always pinning the image tag this run is measuring.

    Injected here rather than left to the values file so that every later
    upgrade — switching an autoscaler on, changing a replica count — cannot
    quietly revert the deployment to a different build.
    """
    overrides = dict(overrides or {})
    if _image_tag:
        overrides.setdefault("image.tag", _image_tag)
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
    for key, value in overrides.items():
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
