"""Sampling profiles of the API worker, and what the samples add up to.

Package 18: every scaling recommendation in this suite rests on
``TAP_CPU_BOUND``, and the only cause that classification ever named — ADQL
translation at 41 ms of a ~50 ms request — was removed by the translation fast
path. What replaced it was nothing: a per-request budget of roughly 10 ms with
1.2 ms of it accounted for.

So the worker is sampled while it is saturated, and the samples are attributed
to named subsystems. Two things make that attribution mean something:

* **The profile runs against the real pod, from outside it.** py-spy attaches
  to the uvicorn process by PID; the image is not modified and nothing is
  imported into the process. A profiler inside the image would be a different
  build from the one every other family measured.
* **The distribution comes from the samples, the total comes from the
  cgroup.** py-spy says where the time goes, not how much there is — a
  sampling profiler's absolute rate depends on its own rate and on how often
  it lost a race. The denominator is Prometheus' CPU accounting for the same
  window, divided by the requests that window served. Mixing the two up is how
  a profile becomes a source of throughput figures it cannot support.

Two passes are taken of every profiled window, and what each is worth is
itself a measured question rather than a designed one:

* ``--gil``: only stacks that hold the GIL. One worker is one interpreter lock,
  so this is the resource the throughput ceiling is made of, and its
  distribution is the attribution this module exists to produce. Its *absolute*
  sample count is not a duration unless the sampler achieved its requested rate
  — which nonblocking sampling does not (see ``summarise``).
* all non-idle threads: intended to add the work done with the GIL released —
  libpq on the socket, the writers' C extensions, the threadpool's handoff. It
  shows some of that (psycopg came out at 11% of it), but it does **not**
  isolate off-GIL CPU the way it was meant to: py-spy counts a thread blocked
  in ``epoll`` as non-idle, so 84% of this pass against a saturated worker was
  the event loop waiting. Read it as a distribution of thread activity. The
  attribution rests on the GIL pass against the cgroup's total.

"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import re
import shutil
import subprocess
import sys
import threading

log = logging.getLogger("egernia_bench.pyspy")

#: The uvicorn command line the chart's API container runs. Matched rather
#: than looked up through the runtime because the profiler runs on the host:
#: a kind node is a container, so its processes are visible in the host's
#: /proc under host PIDs, and those are the PIDs py-spy has to be given.
WORKER_PATTERN = re.compile(r"uvicorn\s+egernia_api\.main:app")


# ---------------------------------------------------------------------------
# Attribution rules
# ---------------------------------------------------------------------------
#
# A sample is attributed to the *innermost* frame that names a subsystem,
# scanning from the leaf towards the root. Leaf self-time is the wrong unit on
# its own: half of a serialiser's cost is stdlib `csv` and `str.join`, and half
# of the translator's is `re`, so a table of leaf frames says "the hot function
# is re.match" and names nothing anybody can act on. Rolling stdlib leaves up
# to the nearest named caller answers the question the package actually asks —
# which *part of the request* the 10 ms belongs to.
#
# Consequently no stdlib module appears below except `logging`, which is a
# subsystem in its own right rather than a helper called by one.
#
# Order is significant: the first rule that matches a frame wins, so the
# specific paths come before the framework catch-alls.
SUBSYSTEMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "adql translation",
        (
            "queryparser/",
            "antlr4/",
            "egernia_core/query/adql.py",
            # The translator is being vendored (ADQL 2.1, package 21), which
            # moves its frames from the `queryparser` package into this tree.
            # Both paths are named so a profile taken before that lands and one
            # taken after are the same breakdown rather than a subsystem that
            # silently emptied into "egernia (other)".
            "egernia_core/query/_adql/",
        ),
    ),
    (
        "result writers",
        (
            "egernia_core/query/results.py",
            "egernia_core/query/votable.py",
            "egernia_core/query/upload.py",
            "pyarrow/",
        ),
    ),
    (
        "psycopg and row conversion",
        ("psycopg/", "psycopg_pool/", "psycopg_binary/"),
    ),
    (
        "token verification",
        (
            "egernia_core/auth/",
            "egernia_api/auth.py",
            "egernia_api/auth_plugins/",
            "jwt/",
            "cryptography/",
        ),
    ),
    (
        "parameter parsing and validation",
        (
            "egernia_api/queries/params.py",
            "egernia_api/queries/uploads.py",
            "python_multipart/",
            "starlette/formparsers.py",
            "starlette/datastructures.py",
        ),
    ),
    (
        "query preparation",
        ("egernia_api/queries/query.py",),
    ),
    (
        "observability",
        (
            "egernia_core/observability.py",
            "prometheus_client/",
            "ska_src_logging/",
            "opentelemetry/",
            "logging/__init__.py",
            "logging/handlers.py",
        ),
    ),
    (
        "threadpool handoff",
        ("anyio/", "concurrent/futures/", "starlette/concurrency.py"),
    ),
    (
        "http server",
        ("uvicorn/", "h11/", "httptools/", "websockets/", "wsproto/"),
    ),
    (
        "asgi routing and dependencies",
        ("fastapi/", "starlette/", "pydantic/", "pydantic_core/"),
    ),
    (
        "event loop",
        ("asyncio/", "selectors.py", "uvloop/"),
    ),
    (
        "egernia (other)",
        ("egernia_api/", "egernia_core/", "egernia_executor/"),
    ),
)

#: Which buckets are the request's own work rather than the machinery that
#: carries it. Reported separately because "88% of the ceiling is
#: unaccounted for" is a claim about the first group, and a breakdown in which
#: the event loop and the HTTP parser absorb most of the time would answer it
#: by relabelling rather than by explaining.
APPLICATION_BUCKETS = frozenset(
    {
        "adql translation",
        "result writers",
        "psycopg and row conversion",
        "token verification",
        "parameter parsing and validation",
        "query preparation",
        "observability",
        "egernia (other)",
    }
)

#: What a frame looks like in py-spy's raw (folded) output: `name (path)`.
_FRAME = re.compile(r"^(?P<function>.*?) \((?P<path>[^)]*)\)$")


def binary() -> str | None:
    """py-spy, preferring the one installed beside this interpreter.

    The suite's own virtualenv is where `docs/python-performance.md` says to
    install it, and `sudo py-spy` would otherwise resolve against root's PATH
    — which on this machine has no py-spy at all.
    """
    venv = pathlib.Path(sys.prefix) / "bin" / "py-spy"
    if venv.exists():
        return str(venv)
    return shutil.which("py-spy")


def worker_pids() -> list[int]:
    """Host PIDs of the API's uvicorn processes, newest first.

    Read from /proc rather than from `docker top` or `crictl`: those report
    PIDs in the node's namespace, and py-spy has to be handed the host's.
    """
    found: list[tuple[float, int]] = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            started = (entry / "stat").stat().st_mtime
        except OSError:
            continue  # exited between listing and reading
        if WORKER_PATTERN.search(cmdline):
            found.append((started, int(entry.name)))
    return [pid for _, pid in sorted(found, reverse=True)]


@dataclasses.dataclass(frozen=True)
class Profile:
    """One py-spy pass: the folded stacks, and what they add up to."""

    pid: int
    gil_only: bool
    nonblocking: bool
    rate: int
    duration_s: float
    samples: int
    errors: int
    path: pathlib.Path
    buckets: dict[str, float]  # bucket -> fraction of samples
    frames: list[tuple[str, float]]  # innermost named frame -> fraction
    unattributed: float

    @property
    def named_fraction(self) -> float:
        return 1.0 - self.unattributed

    @property
    def application_fraction(self) -> float:
        return sum(v for k, v in self.buckets.items() if k in APPLICATION_BUCKETS)

    def as_dict(self) -> dict:
        return {
            "pid": self.pid,
            "gil_only": self.gil_only,
            "nonblocking": self.nonblocking,
            "rate_hz": self.rate,
            "duration_s": self.duration_s,
            "samples": self.samples,
            "errors": self.errors,
            # Stacks py-spy read and threw away. Nonzero only in nonblocking
            # mode, where it is the visible half of that mode's bias.
            "error_fraction": self.errors / max(self.samples + self.errors, 1),
            "raw": self.path.name,
            "buckets": self.buckets,
            "top_frames": [{"frame": name, "fraction": share} for name, share in self.frames],
            "unattributed_fraction": self.unattributed,
            "named_fraction": self.named_fraction,
            "application_fraction": self.application_fraction,
        }


def record(
    pid: int,
    *,
    seconds: float,
    out_path: pathlib.Path,
    rate: int = 100,
    gil_only: bool = False,
    nonblocking: bool = True,
    sudo: bool = True,
) -> tuple[int, int]:
    """Sample `pid` for `seconds`, writing folded stacks. Returns (samples, errors).

    ``nonblocking`` defaults to True, and that default is a measurement rather
    than a preference. Blocking sampling — py-spy's own default — pauses the
    process to walk its stacks, and against a saturated `tap-api` worker at
    100 Hz that cost **74% of its throughput** (95.5 rps unprofiled, 24.9 rps
    profiled) and then got the pod killed: a stalled worker cannot answer
    `/health/live` inside its one-second timeout, so the kubelet restarted a
    process that was busy rather than broken. Both guards fired, which is why
    the number above exists — but a mode that reliably destroys the thing it
    measures is not a default worth keeping.

    What nonblocking costs instead is torn stacks: it reads a stack while the
    interpreter is rewriting it, so py-spy discards the samples it can detect
    as inconsistent (counted, and kept with the profile as `error_fraction`)
    and misattributes the ones it cannot. A known, reported, partial bias in
    the distribution is worth more than an unbiased distribution of a service
    that no longer exists.

    Idle threads are excluded, which is py-spy's default: a thread parked on a
    condition variable is not spending the CPU this is trying to attribute.
    """
    tool = binary()
    if tool is None:
        raise RuntimeError(
            "py-spy is not installed. `uv pip install --python .venv/bin/python py-spy==0.4.2`"
            " — see docs/python-performance.md"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        tool,
        "record",
        "--pid",
        str(pid),
        "--duration",
        str(int(seconds)),
        "--rate",
        str(rate),
        "--format",
        "raw",
        # Line numbers would split one function across every line it can be
        # sampled on, which turns a breakdown into a scatter and changes
        # between builds for reasons that are not performance.
        "--nolineno",
        "--output",
        str(out_path),
    ]
    if gil_only:
        args.append("--gil")
    if nonblocking:
        args.append("--nonblocking")
    if sudo:
        # The worker lives in another PID and mount namespace, so reading its
        # memory needs root on the host. -n: a benchmark must never stop for a
        # password prompt at hour three.
        args = ["sudo", "-n", *args]
    log.info("py-spy: %s", " ".join(args))
    # Popen rather than run(), so a caller can stop it. py-spy under `sudo`
    # outlives the harness otherwise: when the first blocking pass was
    # interrupted, an orphaned profiler kept pausing the worker — and a
    # profiler nobody is waiting for is a profiler that restarts pods.
    process = subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        output = process.communicate(timeout=seconds + 120)[0] or ""
    except subprocess.TimeoutExpired:
        stop(out_path)
        output = process.communicate()[0] or ""
    if not out_path.exists() or not out_path.stat().st_size:
        raise RuntimeError(f"py-spy wrote no profile (exit {process.returncode}):\n{output}")
    samples, errors = _tally(output)
    if process.returncode != 0:
        # py-spy has been seen to exit non-zero *after* writing its output; the
        # CI workflow already judges it by the artefact rather than the status.
        log.warning("py-spy exited %d after writing %s", process.returncode, out_path.name)
    return samples, errors


def stop(out_path: pathlib.Path) -> None:
    """Kill the py-spy that is writing `out_path`, if one still is.

    Matched on the output path because it is unique per pass, and because the
    process to kill is not the one that was spawned: `sudo` is the child, py-spy
    is its child, and signalling `sudo` leaves the profiler attached to the
    worker. Root signals root.
    """
    subprocess.run(
        ["sudo", "-n", "pkill", "-f", str(out_path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _tally(output: str) -> tuple[int, int]:
    """`Samples: N Errors: M` from py-spy's own summary line."""
    match = re.search(r"Samples:\s*(\d+)\s+Errors:\s*(\d+)", output)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


class Pass:
    """One py-spy pass, taken around a measured load window.

    A context manager because that is the shape the window has: `measure()`
    enters this immediately before generating load and exits it when the load
    stops, so the samples cover the measured window and not the warmup, the
    statistics snapshots or the Prometheus collection either side of it.

    The sampler runs in a thread rather than being polled, because py-spy is a
    subprocess with its own duration and the load is in this process's own
    event loop; and it is asked for the window's length so it stops on its own
    if the load overruns. Failures are captured rather than raised: a run that
    measured its throughput and lost its profile still has a measurement, and
    the family reports the missing profile instead of discarding the rung.
    """

    def __init__(
        self,
        *,
        out_path: pathlib.Path,
        seconds: float,
        rate: int = 100,
        gil_only: bool = False,
        nonblocking: bool = True,
        pid: int | None = None,
    ) -> None:
        self.out_path = out_path
        self.seconds = seconds
        self.rate = rate
        self.gil_only = gil_only
        self.nonblocking = nonblocking
        self.pid = pid
        self.error: str = ""
        self.samples = 0
        self.errors = 0
        self._thread = None

    def __enter__(self) -> Pass:
        if self.pid is None:
            pids = worker_pids()
            if len(pids) != 1:
                # More than one and the profile would be of an arbitrary
                # replica while the throughput is of the fleet; none and there
                # is nothing to attach to. Either way, say which.
                self.error = f"expected exactly one tap-api worker process, found {len(pids)}"
                log.error("%s", self.error)
                return self
            self.pid = pids[0]
        self._thread = threading.Thread(target=self._run, name="py-spy", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self.samples, self.errors = record(
                self.pid,
                seconds=self.seconds,
                out_path=self.out_path,
                rate=self.rate,
                gil_only=self.gil_only,
                nonblocking=self.nonblocking,
            )
        except Exception as exc:  # reported on the pass, not raised
            self.error = f"{type(exc).__name__}: {exc}"
            log.error("py-spy pass failed: %s", self.error)

    def __exit__(self, *_exc) -> None:
        if self._thread is None:
            return
        # Generous: py-spy stops itself at --duration, so anything past that is
        # a process that has to be waited out rather than a poll interval to
        # tune.
        self._thread.join(timeout=self.seconds + 120)
        if self._thread.is_alive():
            self.error = self.error or "py-spy did not finish within the window + 120s"
        # Unconditionally, including on the way out of an exception: whatever
        # else went wrong, the one thing that must not survive this block is a
        # profiler still attached to the worker.
        stop(self.out_path)

    @property
    def profile(self) -> Profile | None:
        """The pass as an attributed profile, or None if it produced nothing."""
        if self.error or not self.out_path.exists() or self.pid is None:
            return None
        # errors="replace", not a strict read: a torn nonblocking sample can
        # land a frame name that is not valid UTF-8 (one did — `\xfa\x82\xb8…`
        # where a class name should have been), and a whole ten-minute pass is
        # not worth discarding over two corrupt bytes in two thousand stacks.
        # The mangled stack matches no rule and is counted as such.
        buckets, frames, unattributed = attribute(
            self.out_path.read_text(encoding="utf-8", errors="replace")
        )
        if not buckets and not unattributed:
            return None
        return Profile(
            pid=self.pid,
            gil_only=self.gil_only,
            nonblocking=self.nonblocking,
            rate=self.rate,
            duration_s=self.seconds,
            samples=self.samples,
            errors=self.errors,
            path=self.out_path,
            buckets=buckets,
            frames=frames,
            unattributed=unattributed,
        )


def from_folded(
    path: pathlib.Path,
    *,
    pid: int = 0,
    gil_only: bool = False,
    nonblocking: bool = True,
    rate: int = 100,
    duration_s: float = 0.0,
) -> Profile | None:
    """Rebuild a profile from stacks already on disk.

    A ten-minute pass is too expensive to lose to a resume. The measurement's
    marker is written before the profile is attached to it, so a run that
    resumed replayed the rung from cache and then reported "no profile" while
    the folded stacks sat beside it — which is the opposite of this suite's
    rule that analysis is re-derivable from artefacts.

    ``samples`` is recovered by summing the file's own counts, which is exactly
    what py-spy wrote. ``errors`` cannot be: it only ever existed in the
    profiler's stdout summary, so it comes back as 0 and the run's
    ``achieved_sample_rate_hz`` is the figure to judge the pass by.
    """
    if not path.exists() or not path.stat().st_size:
        return None
    folded = path.read_text(encoding="utf-8", errors="replace")
    buckets, frames, unattributed = attribute(folded)
    if not buckets and not unattributed:
        return None
    samples = 0
    for line in folded.splitlines():
        _stack, _, count = line.strip().rpartition(" ")
        try:
            samples += int(count)
        except ValueError:
            continue
    return Profile(
        pid=pid,
        gil_only=gil_only,
        nonblocking=nonblocking,
        rate=rate,
        duration_s=duration_s,
        samples=samples,
        errors=0,
        path=path,
        buckets=buckets,
        frames=frames,
        unattributed=unattributed,
    )


def attribute(
    folded: str, *, top: int = 25
) -> tuple[dict[str, float], list[tuple[str, float]], float]:
    """Fold py-spy's raw output into subsystem shares.

    Returns (bucket fractions, the busiest named frames, the unattributed
    fraction). Fractions are of total samples, so they sum to 1 within
    rounding — a share of "the CPU this profile saw", which is the only thing
    a sampling profiler can honestly report.
    """
    bucket_counts: dict[str, int] = {}
    frame_counts: dict[str, int] = {}
    unattributed = 0
    corrupt = 0
    total = 0
    for line in folded.splitlines():
        line = line.strip()
        if not line:
            continue
        stack, _, count_text = line.rpartition(" ")
        try:
            count = int(count_text)
        except ValueError:
            continue
        if not stack:
            # A count with no stack in front of it: a torn write that lost the
            # frames but kept the tally (one line of `193` in the published
            # pass). Counted into the total and into the residual, not skipped —
            # dropping it would shrink the denominator that "how much did we
            # attribute?" is a fraction of, which is the one number this family
            # exists to stop being flattering.
            total += count
            unattributed += count
            continue
        total += count
        if "\ufffd" in stack:
            # A torn read that produced bytes rather than a frame name. Counted
            # rather than dropped: it is the visible part of nonblocking
            # sampling's bias, and a bias worth reporting is worth measuring.
            corrupt += count
        bucket, frame = _classify(stack.split(";"))
        if bucket is None:
            unattributed += count
            continue
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + count
        frame_counts[frame] = frame_counts.get(frame, 0) + count
    if not total:
        return {}, [], 0.0
    if corrupt:
        log.warning(
            "%d of %d samples had a frame name that was not valid UTF-8 (torn reads)",
            corrupt,
            total,
        )
    buckets = {
        name: count / total for name, count in sorted(bucket_counts.items(), key=lambda kv: -kv[1])
    }
    frames = [
        (name, count / total)
        for name, count in sorted(frame_counts.items(), key=lambda kv: -kv[1])[:top]
    ]
    return buckets, frames, unattributed / total


def _classify(stack: list[str]) -> tuple[str | None, str]:
    """The innermost frame that names a subsystem, and that subsystem.

    Innermost, not outermost: every stack in this process is rooted in
    uvicorn, so matching from the root would attribute the entire profile to
    the HTTP server and say nothing.
    """
    for frame in reversed(stack):
        match = _FRAME.match(frame)
        path = match.group("path") if match else frame
        for name, needles in SUBSYSTEMS:
            if any(needle in path for needle in needles):
                return name, frame
    return None, stack[-1] if stack else ""


def summarise(
    profile: Profile,
    *,
    requests: int,
    window_s: float,
    cpu_cores_mean: float,
) -> dict:
    """What the profile says a request costs, and in what.

    Three per-request figures, deliberately not one:

    ``cgroup_cpu_ms`` is CPU accounting divided by requests served — the
    honest total, and the one the ``TAP_CPU_BOUND`` verdict is derived from.

    ``by_subsystem_ms`` splits the cgroup total by the profile's shares. That
    is the number the package asks for, and it is a product of two
    measurements rather than either one alone.

    ``profiled_occupancy_ms`` — the sample count over the sampling rate, over
    the requests — is only a quantity when the profiler achieved the rate it
    was asked for. Blocking sampling does; nonblocking sampling does not — it
    reached ~41 Hz of a requested 100 over a ten-minute window here. Read as
    interpreter-lock occupancy per request that would be out by a factor of two
    and a half, so it is ``None`` whenever the sampler missed its rate and the
    achieved rate is reported in its place. What the shares still support is the
    *split*; what they no longer support is the total, and the total is what the
    cgroup is for.
    """
    achieved_hz = profile.samples / profile.duration_s if profile.duration_s else 0.0
    cgroup_cpu_ms = 1000.0 * cpu_cores_mean * window_s / requests if requests else 0.0
    # A rate the sampler did not achieve cannot be divided by. The threshold is
    # deliberately loose: this asks "did the profiler roughly keep up?", it does
    # not measure the profiler.
    rate_achieved = bool(profile.rate) and achieved_hz >= 0.5 * profile.rate
    occupancy_ms = (
        1000.0 * (profile.samples / profile.rate) / requests if rate_achieved and requests else None
    )
    return {
        **profile.as_dict(),
        "requests": requests,
        "window_s": window_s,
        "cpu_cores_mean": cpu_cores_mean,
        "cgroup_cpu_ms_per_request": cgroup_cpu_ms,
        "achieved_sample_rate_hz": achieved_hz,
        "profiled_occupancy_ms_per_request": occupancy_ms,
        "occupancy_unavailable_reason": (
            None
            if occupancy_ms is not None
            else "every non-idle thread was sampled, and a thread blocked in epoll counts"
            " as non-idle, so this pass's sample count is thread-activity time rather than"
            " processor time"
            if not profile.gil_only
            else f"the sampler achieved {achieved_hz:.1f} Hz of the {profile.rate} Hz asked"
            f" for ({100 * profile.errors / max(profile.samples + profile.errors, 1):.0f}%"
            " of reads discarded as torn), so its sample count is not a duration"
        ),
        "by_subsystem_ms": {name: share * cgroup_cpu_ms for name, share in profile.buckets.items()},
    }
