"""Bring up, configure and interrogate the benchmark cluster.

Every step here is one the numbers depend on, which is why they are code and
not a README: the node's CPU cap, the load generator living outside it, and
the exact chart values are as much a part of a result as the request latency
is.
"""

from __future__ import annotations

import atexit
import json
import logging
import pathlib
import shutil
import socket
import subprocess
import time

log = logging.getLogger("egernia_bench.cluster")

REPO = pathlib.Path(__file__).resolve().parents[3]
SUITE = pathlib.Path(__file__).resolve().parents[1]
CLUSTER = "egernia-bench"
NODE_CONTAINER = f"{CLUSTER}-control-plane"
RELEASE = "egernia"
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
    "tap-api": ("services/egernia-api/Dockerfile", "."),
    "tap-executor": ("services/egernia-executor/Dockerfile", "."),
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
        staging = f"egernia-bench/{name}:staging"
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
        run("docker", "tag", f"egernia-bench/{name}:staging", f"egernia-bench/{name}:{tag}")
        run(
            "kind",
            "load",
            "docker-image",
            f"egernia-bench/{name}:{tag}",
            "--name",
            CLUSTER,
            timeout=900,
        )
    _image_tag = tag
    log.info("images built and loaded as %s", tag)
    return tag, {f"egernia-bench/{name}:{tag}": digest for name, digest in digests.items()}


def use_image_tag(tag: str | None) -> None:
    """Pin the tag every later upgrade has to keep deploying.

    Without this, a run that skips the build reads the deployed tag, passes it
    to the first install, and then loses it: the next upgrade — switching an
    autoscaler on, changing a replica count — falls back to the values file's
    mutable ``:bench`` and silently redeploys a different build mid-run. The
    image guard caught exactly that.
    """
    global _image_tag
    _image_tag = tag


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


def verify_running_images(expected_tag: str, timeout_s: float = 180.0) -> None:
    """Refuse to measure pods that are not running the images just built.

    The check is on the pod spec rather than on an image id, because kind
    rewrites ids on import — the id in a pod's status bears no relation to the
    one docker built, so comparing those would look like a check and verify
    nothing.

    Waits rather than sampling once. A rolling update leaves the previous pod
    present and Terminating for a few seconds after the new one is Ready, and a
    guard that fails on that is a guard that fails on every successful
    deployment. Terminating pods are skipped and the rest are re-checked until
    they agree or the deadline passes — so the guard is strict about the end
    state and patient about how it is reached.
    """
    deadline = time.monotonic() + timeout_s
    wrong: list[str] = []
    while time.monotonic() < deadline:
        payload = json.loads(
            kubectl("get", "pods", "-l", f"app.kubernetes.io/instance={RELEASE}", "-o", "json")
        )
        wrong = []
        for item in payload.get("items", []):
            if item["metadata"].get("deletionTimestamp"):
                continue  # on its way out; not what will serve the measurement
            for container in item["spec"]["containers"]:
                if expected_tag not in container["image"]:
                    wrong.append(f"{item['metadata']['name']}: {container['image']}")
        if not wrong:
            log.info("all pods confirmed running %s", expected_tag)
            return
        log.info("waiting for the rollout to %s (%d pod(s) behind)", expected_tag, len(wrong))
        time.sleep(5)
    raise RuntimeError(
        "these pods are still not running the images this run built "
        f"(expected tag {expected_tag}) after {timeout_s:.0f}s:\n  " + "\n  ".join(wrong)
    )


#: KEDA's images, and the tag the chart asks for. Pre-loaded into the node the
#: same way the service's are, because the node's containerd reaches the
#: internet on its own and a host behind a proxy or an image mirror can pull
#: what the node cannot. The chart defaults to `imagePullPolicy: Always`,
#: which turns a loaded image back into a pull, so the policy is overridden to
#: match — the version is pinned either way, so "Always" was buying nothing.
KEDA_IMAGES = (
    "ghcr.io/kedacore/keda",
    "ghcr.io/kedacore/keda-metrics-apiserver",
    "ghcr.io/kedacore/keda-admission-webhooks",
)


def load_public_images(references: list[str]) -> list[str]:
    """Pull images on the host and import them into the node.

    Returns the references that could not be made available, so a caller can
    decide whether that is fatal. `docker save --platform` rather than
    `kind load docker-image`: with Docker's containerd image store, a
    multi-platform reference saves manifests whose blobs are not local, and
    the node's importer rejects the archive for a digest it cannot find.
    """
    import tempfile

    missing: list[str] = []
    for reference in references:
        if run("docker", "image", "inspect", reference, check=False).startswith("["):
            pass  # already on the host
        elif run("docker", "pull", "--platform", "linux/amd64", reference, check=False).find(
            "Downloaded"
        ) < 0 and not run("docker", "image", "inspect", reference, check=False).startswith("["):
            log.warning("could not pull %s on the host", reference)
            missing.append(reference)
            continue
        with tempfile.NamedTemporaryFile(suffix=".tar") as archive:
            run(
                "docker",
                "save",
                "--platform",
                "linux/amd64",
                reference,
                "-o",
                archive.name,
                timeout=900,
            )
            output = run(
                "kind",
                "load",
                "image-archive",
                archive.name,
                "--name",
                CLUSTER,
                check=False,
                timeout=900,
            )
        if "error" in output.lower():
            log.warning("could not import %s into the node: %s", reference, output.strip())
            missing.append(reference)
        else:
            log.info("imported %s into the node", reference)
    return missing


def install_keda() -> str:
    """Install KEDA, pinned, and return the version actually running."""
    if "keda" not in kubectl("get", "ns", "-o", "name", check=False):
        run("helm", "repo", "add", "kedacore", "https://kedacore.github.io/charts", check=False)
        run("helm", "repo", "update", "kedacore", check=False)
        # Best effort: if the node can pull ghcr.io itself, this is a warm
        # cache; if it cannot, it is the only way the install completes. Either
        # way a failure here is not fatal — the helm install below is what says
        # whether KEDA came up.
        preloaded = not load_public_images([f"{name}:{KEDA_VERSION}" for name in KEDA_IMAGES])
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
            *(
                (
                    "--set",
                    "image.pullPolicy=IfNotPresent",
                    "--set",
                    "image.keda.pullPolicy=IfNotPresent",
                    "--set",
                    "image.metricsApiServer.pullPolicy=IfNotPresent",
                    "--set",
                    "image.webhooks.pullPolicy=IfNotPresent",
                )
                if preloaded
                else ()
            ),
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


#: Read out of manifests/monitoring.yaml rather than restated, so the list
#: cannot drift from what the manifest actually deploys.
def monitoring_images() -> list[str]:
    text = (SUITE / "manifests/monitoring.yaml").read_text()
    return sorted(
        {
            line.split("image:", 1)[1].strip()
            for line in text.splitlines()
            if line.strip().startswith("image:")
        }
    )


def install_monitoring() -> None:
    # Same reason as KEDA's. No pull-policy override needed here: both images
    # carry an explicit version tag, and Kubernetes defaults anything but
    # `:latest` to IfNotPresent — so a pre-loaded image is used and a node with
    # no registry access still gets a Prometheus.
    load_public_images(monitoring_images())
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
        str(REPO / "deploy/helm/egernia"),
        "--kube-context",
        f"kind-{CLUSTER}",
        "--values",
        str(SUITE / "config/chart-values.yaml"),
        "--wait",
        "--timeout",
        "10m",
        # scale() sets replicas through the scale subresource, which leaves
        # kubectl owning that field. Helm applies server-side, so the next
        # upgrade — the one switching an autoscaler on for the KEDA family —
        # aborts on the conflict instead. The chart is the authority on every
        # field it renders, so take the field back rather than fail.
        "--force-conflicts",
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


def set_limit_concurrency(value: int) -> None:
    """Set uvicorn's per-worker connection ceiling on tap-api; 0 clears it.

    Goes through the chart rather than `kubectl set env` so the values file
    stays the single authority on what is deployed — and because a helm
    upgrade resets every override the previous upgrade set, the caller must
    re-apply replica counts afterwards.

    That reset is total, not just of replicas: this upgrade carries one
    `--set`, so it also discards the `horizontalAutoscaling.*=false` flags
    `set_autoscaling()` applied before the shedding loop. It is only harmless
    because config/chart-values.yaml has both autoscalers off anyway — flip
    that file and the second rung of the shedding ladder would silently run
    against an autoscaling fleet. Anything a caller needs to survive a flip
    belongs in the suite values file, or must be re-applied after every one.
    """
    install_chart({"tapApi.limitConcurrency": str(value)})


def set_workers(value: int) -> None:
    """Set uvicorn worker processes per tap-api pod; the chart's own knob.

    Same route and same caveats as `set_limit_concurrency`: the values file
    stays the single authority on what is deployed, a helm upgrade resets
    every override the previous one set, and it reverts the replica count to
    the values file's — so the caller must re-apply `scale()` after every
    call. The value reaches the pods through the ConfigMap, and the pod
    template hashes the ConfigMap (checksum/config), so the upgrade is a
    rollout `--wait` waits out rather than a config change no running pod has
    read.
    """
    install_chart({"tapApi.workers": str(value)})
    verify_workers(value)


def verify_workers(expected: int, timeout_s: float = 120.0) -> None:
    """Every ready tap-api pod started with `expected` in TAP_API_WORKERS.

    Asked of the pods rather than read back from the ConfigMap, because the
    failure this guards against is exactly the config saying one thing while
    the running processes say another: the pods read TAP_API_WORKERS once, at
    startup (it is uvicorn's `--workers`), so before the pod template hashed
    the ConfigMap a workers change was an upgrade helm reported successful
    and no running pod had read — a whole grid would have measured workers=1
    twelve times over.
    """
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        pods = json.loads(
            kubectl(
                "get",
                "pods",
                "-l",
                f"app.kubernetes.io/instance={RELEASE},app.kubernetes.io/component=tap-api",
                "-o",
                "json",
            )
        )["items"]
        ready = [
            p["metadata"]["name"]
            for p in pods
            if not p["metadata"].get("deletionTimestamp")
            and any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in p["status"].get("conditions", [])
            )
        ]
        seen = {
            name: kubectl(
                "exec", name, "-c", "tap-api", "--", "printenv", "TAP_API_WORKERS"
            ).strip()
            for name in ready
        }
        stale = {name: value for name, value in seen.items() if value != str(expected)}
        if ready and not stale:
            return
        last = f"ready={ready} TAP_API_WORKERS={seen}"
        time.sleep(3)
    raise RuntimeError(
        f"tap-api pods did not come up with TAP_API_WORKERS={expected} within {timeout_s}s: {last}"
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


#: One port-forward for the whole process, torn down at exit.
_forward: subprocess.Popen | None = None


def _port_accepts(host: str = "127.0.0.1", port: int = 55433) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((host, port)) == 0


def _database_answers(timeout_s: float) -> bool:
    """A real handshake through the forward, not just an accepting socket.

    An accepting socket is not a serving database: a forwarder terminated by
    a previous benchmark process keeps accepting for a moment while it dies,
    and a run that adopted the port in that window opened its generation
    connection into a proxy with no backend — "server closed the connection
    unexpectedly", after helm had reported every pod ready.
    """
    import psycopg

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(database_dsn(), connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return True
        except psycopg.OperationalError:
            time.sleep(1.0)
    return False


def port_forward_database(timeout_s: float = 60.0) -> subprocess.Popen:
    """Ensure the database is reachable on the host, once per process.

    Started once and reused. One forward per measurement — started, slept on
    for three seconds, terminated — cost eight and a half minutes between two
    measurements: a terminated forward does not release the port immediately,
    the replacement cannot bind, and the wait for it was neither bounded nor
    logged. Thirty measurements paid that.

    Readiness is established by connecting to the port rather than by sleeping.
    A fixed sleep is either too short, which fails, or too long, which is the
    thing being fixed.
    """
    global _forward
    if _forward is not None and _forward.poll() is None and _port_accepts():
        return _forward
    if _forward is not None:
        _forward.terminate()
        _forward = None
    if _port_accepts():
        # Something already forwards this port — another benchmark process, or
        # a forward this process started and lost track of. Spawning a second
        # one would leave a doomed kubectl that cannot bind — but adopting the
        # port is only safe once the database answers through it: a forwarder
        # a finished process just terminated keeps accepting while it dies.
        if _database_answers(10.0):
            log.debug("database port already forwarded by another process")
            return subprocess.Popen(["true"])
        log.info("port 55433 accepts but the database does not answer; waiting for it to free")
        deadline = time.monotonic() + timeout_s
        while _port_accepts() and time.monotonic() < deadline:
            time.sleep(1.0)
        if _port_accepts():
            raise RuntimeError(
                "port 55433 is held by a forward the database does not answer through"
            )
    _forward = subprocess.Popen(
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
    atexit.register(close_database_forward)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        # The handshake, not just the open port: our own fresh forward can
        # also front a database that is still starting.
        if _port_accepts() and _database_answers(min(10.0, deadline - time.monotonic())):
            log.debug("database port-forward ready")
            return _forward
        if _forward.poll() is not None:
            raise RuntimeError("kubectl port-forward exited before the port opened")
        time.sleep(0.25)
    raise RuntimeError(f"database port-forward did not open in {timeout_s:.0f}s")


def close_database_forward() -> None:
    """Tear the shared forward down. Idempotent; registered at exit."""
    global _forward
    if _forward is not None and _forward.poll() is None:
        _forward.terminate()
    _forward = None


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
