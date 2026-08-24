"""Load generation: closed-loop concurrency and open-loop arrival rate.

Both, because they answer different questions and neither answers the other's.
Closed loop (N clients, each issuing the next request when the last returns)
finds the service's capacity and its latency under a known parallelism.
Open loop (arrivals at a rate, regardless of whether the service is keeping
up) is the only way to see a queue build, which is the whole subject of the
autoscaling scenarios — a closed-loop client cannot overload anything, because
it slows down exactly as much as the service does.

Every request is recorded individually. Aggregates are computed later, from
the samples on disk, so a percentile can be recomputed without re-running
anything and a suspicious summary can always be taken back to the requests it
came from.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import os
import time
import typing

import httpx
import psutil

from .. import corpus as corpus_mod

log = logging.getLogger("tapbench.load")

# Where the response's own timing lands: the service returns the correlation
# id it used, which is what ties a slow request here to a statement in
# pg_stat_activity and a line in the executor's log.
REQUEST_ID_HEADER = "X-Request-ID"


@dataclasses.dataclass(slots=True)
class Sample:
    """One request. The unit of everything downstream."""

    t_start: float  # unix seconds, when the request was issued
    t_offered: float  # when it *should* have been issued (open loop)
    query_class: str
    query_id: str
    status: int  # 0 for a transport failure, else HTTP status
    error: str  # "" when fine; the exception class otherwise
    latency_s: float  # issue -> last byte
    ttfb_s: float  # issue -> first byte
    response_bytes: int
    rows: int  # -1 when not counted
    pod: str  # "" when the service does not say
    mode: str  # sync | async
    request_id: str


class Recorder:
    """Collects samples and watches the generator's own health.

    The self-monitoring is not decoration. A load generator pinned at 100% CPU
    reports its own ceiling as the service's, and that mistake is invisible in
    the result — the throughput curve simply flattens, exactly as it would for
    a saturated server. So its CPU is sampled alongside the requests and any
    run where it ran hot is marked rather than believed.
    """

    def __init__(self) -> None:
        self.samples: list[Sample] = []
        self.process = psutil.Process(os.getpid())
        self.cpu_samples: list[tuple[float, float, float]] = []
        self._cpu_count = os.cpu_count() or 1

    def add(self, sample: Sample) -> None:
        self.samples.append(sample)

    async def watch_self(self, stop: asyncio.Event, interval: float = 2.0) -> None:
        self.process.cpu_percent(None)  # prime the counter
        while not stop.is_set():
            await asyncio.sleep(interval)
            # Per-core fraction for the process, and the whole host, so a
            # generator that is fine but a laptop that is not can be told
            # apart.
            self.cpu_samples.append(
                (
                    time.time(),
                    self.process.cpu_percent(None) / 100.0,
                    psutil.cpu_percent(None) / 100.0,
                )
            )

    @property
    def generator_cpu_peak(self) -> float:
        """Peak generator CPU as a fraction of one core's worth per core."""
        if not self.cpu_samples:
            return 0.0
        return max(s[1] for s in self.cpu_samples) / self._cpu_count


class Workload:
    """Draws queries from the corpus according to the mix.

    Deterministic: the same seed issues the same sequence of queries in the
    same order, so two runs differ in what the service did rather than in what
    it was asked to do.
    """

    def __init__(
        self, entries: list[corpus_mod.CorpusEntry], mix: dict[str, float], seed: int
    ) -> None:
        self.by_class = corpus_mod.by_class(entries)
        missing = [c for c in mix if c not in self.by_class]
        if missing:
            raise ValueError(f"mix names classes absent from the corpus: {missing}")
        self.classes = list(mix)
        total = sum(mix.values())
        # Cumulative weights, normalised: a mix that does not sum to 1 is a
        # typo, not an instruction, but normalising is kinder than failing on
        # a rounding error in YAML.
        self.cumulative: list[float] = []
        running = 0.0
        for cls in self.classes:
            running += mix[cls] / total
            self.cumulative.append(running)
        self.rng = corpus_mod.Deterministic(seed)

    def next(self) -> corpus_mod.CorpusEntry:
        pick = self.rng._next()
        for cls, edge in zip(self.classes, self.cumulative, strict=True):
            if pick <= edge:
                pool = self.by_class[cls]
                return pool[self.rng.randint(0, len(pool) - 1)]
        return self.by_class[self.classes[-1]][0]


class SingleClass(Workload):
    """One query class only, for the stress runs."""

    def __init__(self, entries: list[corpus_mod.CorpusEntry], query_class: str, seed: int) -> None:
        super().__init__(entries, {query_class: 1.0}, seed)


async def _issue_sync(
    client: httpx.AsyncClient,
    base_url: str,
    entry: corpus_mod.CorpusEntry,
    offered_at: float,
    recorder: Recorder,
    response_format: str,
) -> None:
    """One /tap/sync request, timed to the last byte."""
    started = time.time()
    t0 = time.perf_counter()
    ttfb = math.nan
    size = 0
    status = 0
    error = ""
    pod = ""
    request_id = ""
    try:
        async with client.stream(
            "POST",
            f"{base_url}/tap/sync",
            data={"LANG": "ADQL", "QUERY": entry.adql, "RESPONSEFORMAT": response_format},
        ) as response:
            status = response.status_code
            request_id = response.headers.get(REQUEST_ID_HEADER, "")
            # Only if the deployment chose to say; the service does not
            # advertise its pod name, so this is usually empty and per-pod
            # attribution comes from Prometheus instead.
            pod = response.headers.get("X-Pod-Name", "")
            async for chunk in response.aiter_bytes():
                if math.isnan(ttfb):
                    ttfb = time.perf_counter() - t0
                size += len(chunk)
    except Exception as exc:  # a transport failure is a result, not a crash
        error = type(exc).__name__
    latency = time.perf_counter() - t0
    recorder.add(
        Sample(
            t_start=started,
            t_offered=offered_at,
            query_class=entry.query_class,
            query_id=entry.query_id,
            status=status,
            error=error,
            latency_s=latency,
            ttfb_s=(0.0 if math.isnan(ttfb) else ttfb),
            response_bytes=size,
            rows=-1,
            pod=pod,
            mode="sync",
            request_id=request_id,
        )
    )


async def _issue_async(
    client: httpx.AsyncClient,
    base_url: str,
    entry: corpus_mod.CorpusEntry,
    offered_at: float,
    recorder: Recorder,
    poll_interval: float = 0.25,
    timeout_s: float = 600.0,
) -> None:
    """One UWS job, timed from submission to a terminal phase.

    This is the load the KEDA scenarios need: the repository's ScaledObject
    scales executors on the age of the oldest queued job, so only work that
    creates jobs can move it. A sync query never touches that queue.
    """
    started = time.time()
    t0 = time.perf_counter()
    status = 0
    error = ""
    ttfb = math.nan
    size = 0
    request_id = ""
    try:
        created = await client.post(
            f"{base_url}/tap/async",
            data={"LANG": "ADQL", "QUERY": entry.adql, "PHASE": "RUN"},
            follow_redirects=False,
        )
        status = created.status_code
        request_id = created.headers.get(REQUEST_ID_HEADER, "")
        location = created.headers.get("location", "")
        if not location:
            error = "no-job-location"
        else:
            ttfb = time.perf_counter() - t0  # the job exists from here
            deadline = t0 + timeout_s
            phase = "PENDING"
            while time.perf_counter() < deadline:
                phase_response = await client.get(f"{location}/phase")
                phase = phase_response.text.strip()
                if phase in ("COMPLETED", "ERROR", "ABORTED"):
                    break
                await asyncio.sleep(poll_interval)
            if phase == "COMPLETED":
                result = await client.get(f"{location}/results/result")
                size = len(result.content)
                status = result.status_code
            else:
                error = f"phase-{phase}"
                status = 0 if phase not in ("ERROR", "ABORTED") else 500
    except Exception as exc:
        error = type(exc).__name__
    recorder.add(
        Sample(
            t_start=started,
            t_offered=offered_at,
            query_class=entry.query_class,
            query_id=entry.query_id,
            status=status,
            error=error,
            latency_s=time.perf_counter() - t0,
            ttfb_s=(0.0 if math.isnan(ttfb) else ttfb),
            response_bytes=size,
            rows=-1,
            pod="",
            mode="async",
            request_id=request_id,
        )
    )


def _client(concurrency: int, timeout_s: float) -> httpx.AsyncClient:
    # Connection limits above the offered concurrency, and keep-alive on: a
    # generator that reconnects per request measures TCP setup, and one that
    # queues internally on a small pool measures its own pool.
    limits = httpx.Limits(
        max_connections=max(concurrency * 2, 64),
        max_keepalive_connections=max(concurrency * 2, 64),
        keepalive_expiry=60.0,
    )
    return httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(timeout_s), http2=False)


async def closed_loop(
    base_url: str,
    workload: Workload,
    concurrency: int,
    warmup_s: float,
    measure_s: float,
    *,
    mode: str = "sync",
    response_format: str = "csv",
    timeout_s: float = 120.0,
) -> tuple[Recorder, float]:
    """N clients, each issuing the next request as soon as the last finishes.

    Returns the recorder and how long the measured phase actually took. Those
    are not the same as the requested duration: a worker only checks the clock
    between requests, and on an I/O-bound dataset a streaming response can
    drip for minutes, so a phase can overrun its window substantially. Dividing
    the request count by the *requested* duration would then overstate
    throughput by whatever the overrun was.
    """
    recorder = Recorder()
    warm = Recorder()  # discarded: it exists to fill caches and pools
    stop = asyncio.Event()

    async with _client(concurrency, timeout_s) as client:
        watcher = asyncio.create_task(recorder.watch_self(stop))

        async def worker(target: Recorder, until: float) -> None:
            while time.perf_counter() < until:
                entry = workload.next()
                now = time.time()
                if mode == "sync":
                    await _issue_sync(client, base_url, entry, now, target, response_format)
                else:
                    await _issue_async(client, base_url, entry, now, target)

        if warmup_s > 0:
            # Cancelled at the deadline rather than waited out. The warmup's
            # results are discarded anyway, and a single slow-streaming
            # response would otherwise hold the whole run for as long as it
            # keeps trickling — which is how a 60-second warmup became 26
            # minutes of unexplained wall clock.
            until = time.perf_counter() + warmup_s
            warmers = [asyncio.create_task(worker(warm, until)) for _ in range(concurrency)]
            _, pending = await asyncio.wait(warmers, timeout=warmup_s + 30.0)
            for task in pending:
                task.cancel()
            if pending:
                log.info("cancelled %d warmup worker(s) still in a request", len(pending))
            await asyncio.gather(*warmers, return_exceptions=True)
        measured_start = time.perf_counter()
        until = measured_start + measure_s
        await asyncio.gather(*(worker(recorder, until) for _ in range(concurrency)))
        measured_elapsed = time.perf_counter() - measured_start
        stop.set()
        await asyncio.gather(watcher)
    overrun = measured_elapsed - measure_s
    log.info(
        "closed loop c=%d: %d requests in %.1fs (%+.1fs), %.1f rps, generator CPU peak %.0f%%",
        concurrency,
        len(recorder.samples),
        measured_elapsed,
        overrun,
        len(recorder.samples) / measured_elapsed if measured_elapsed else 0.0,
        100 * recorder.generator_cpu_peak,
    )
    if overrun > 0.1 * measure_s:
        log.warning(
            "measured phase overran its window by %.0fs: a worker was inside a "
            "long streaming response when the clock ran out",
            overrun,
        )
    return recorder, measured_elapsed


@dataclasses.dataclass
class Step:
    """A phase of an open-loop profile."""

    seconds: float
    rate: float  # requests/second at the start
    rate_end: float | None = None  # linear ramp to this rate, if given

    def rate_at(self, elapsed: float) -> float:
        if self.rate_end is None or self.seconds <= 0:
            return self.rate
        fraction = min(1.0, max(0.0, elapsed / self.seconds))
        return self.rate + (self.rate_end - self.rate) * fraction


async def open_loop(
    base_url: str,
    workload: Workload,
    steps: list[Step],
    *,
    mode: str = "sync",
    response_format: str = "csv",
    timeout_s: float = 600.0,
    arrival_seed: int = 90210,
    max_in_flight: int = 4096,
) -> tuple[Recorder, list[dict]]:
    """Arrivals at a schedule, whether or not the service keeps up.

    Poisson arrivals rather than a fixed spacing: real traffic clumps, and a
    perfectly even arrival train hides queueing that a real one would provoke.

    Requests are launched into the background and never waited for by the
    arrival loop; if the service stalls, arrivals keep arriving and the
    backlog grows. That is the point — this is the only load shape that can
    make a queue, and a queue is what the autoscaler is watching. `t_offered`
    records when each request *should* have gone out, so the resulting
    coordinated-omission gap is measurable rather than silently absorbed.
    """
    recorder = Recorder()
    stop = asyncio.Event()
    timeline: list[dict] = []
    in_flight: set[asyncio.Task] = set()
    dropped = 0

    peak_rate = max(max(s.rate, s.rate_end or s.rate) for s in steps) if steps else 1.0
    async with _client(int(peak_rate * 4) + 16, timeout_s) as client:
        watcher = asyncio.create_task(recorder.watch_self(stop))
        # A fixed seed, not hash("arrivals"): str hashing is randomised per
        # process, so that would have made the arrival pattern differ between
        # runs of an otherwise identical scenario — the one thing this suite
        # promises it does not do.
        rng = corpus_mod.Deterministic(arrival_seed)
        wall_start = time.time()
        perf_start = time.perf_counter()

        for index, step in enumerate(steps):
            step_start = time.perf_counter()
            next_arrival = step_start
            timeline.append(
                {
                    "step": index,
                    "t": time.time(),
                    "rate": step.rate,
                    "rate_end": step.rate_end,
                    "seconds": step.seconds,
                }
            )
            while True:
                elapsed = time.perf_counter() - step_start
                if elapsed >= step.seconds:
                    break
                rate = step.rate_at(elapsed)
                if rate <= 0:
                    await asyncio.sleep(min(0.25, step.seconds - elapsed))
                    continue
                # Exponential inter-arrival times: a Poisson process.
                gap = -math.log(max(rng._next(), 1e-12)) / rate
                next_arrival += gap
                delay = next_arrival - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                if len(in_flight) >= max_in_flight:
                    # The generator is out of room. Counted, not silently
                    # skipped: an offered rate the generator failed to offer
                    # is a property of the generator and has to reach the
                    # report.
                    dropped += 1
                    continue
                entry = workload.next()
                # When this request should have been issued, on the same clock
                # as t_start: the difference between the two is the generator's
                # own lateness, and hiding it would be coordinated omission.
                offered = wall_start + (next_arrival - perf_start)
                if mode == "sync":
                    task = asyncio.create_task(
                        _issue_sync(client, base_url, entry, offered, recorder, response_format)
                    )
                else:
                    task = asyncio.create_task(
                        _issue_async(client, base_url, entry, offered, recorder)
                    )
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)

        # Let what is already in flight finish, but not forever: a run that
        # ends with requests still queued should say so rather than wait for a
        # timeout each.
        if in_flight:
            await asyncio.wait(set(in_flight), timeout=timeout_s)
        stop.set()
        await asyncio.gather(watcher)

    if dropped:
        log.warning("generator could not offer %d arrivals (in-flight cap)", dropped)
    timeline.append({"step": -1, "t": time.time(), "dropped_arrivals": dropped})
    return recorder, timeline


def samples_to_arrow(samples: typing.Sequence[Sample]):
    """Samples as an Arrow table, for Parquet."""
    import pyarrow as pa

    columns = {name: [] for name in Sample.__slots__}
    for sample in samples:
        for name in Sample.__slots__:
            columns[name].append(getattr(sample, name))
    return pa.table(columns)


def write_samples(samples: typing.Sequence[Sample], path) -> None:
    import pyarrow.parquet as pq

    pq.write_table(samples_to_arrow(samples), path, compression="zstd")
