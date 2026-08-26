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
import concurrent.futures
import contextlib
import dataclasses
import logging
import math
import multiprocessing
import os
import time
import typing
import urllib.parse

import httpx
import psutil

from .. import corpus as corpus_mod

log = logging.getLogger("egernia_bench.load")

# Where the response's own timing lands: the service returns the correlation
# id it used, which is what ties a slow request here to a statement in
# pg_stat_activity and a line in the executor's log.
REQUEST_ID_HEADER = "X-Request-ID"

# Which replica answered, in that replica's own words. The service has sent
# this since it started setting `SERVED_BY_HEADER` — precisely so that "which
# pod served this request" stops being an inference — and this generator spent
# that whole time reading `X-Pod-Name`, a header nothing sends. So every sample
# recorded an empty pod, the code here concluded "the service does not
# advertise its pod name", and per-pod attribution was left to Prometheus,
# which can say how busy each pod was but not which pod answered *this*
# request. Two commits that never met.
#
# It matters most where it was missed: a closed-loop client holds keep-alive
# connections, so kube-proxy assigns each client to a pod once per connection
# and not per request. At low concurrency against several replicas that is a
# coin flip which then persists for the whole window — two clients that land on
# one pod measure one pod's throughput and the rung reads half. Populating this
# makes that visible per measurement instead of something the median of three
# repetitions absorbs.
SERVED_BY_HEADER = "X-Served-By"


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
        """Peak generator CPU as a fraction of ONE core.

        The generator is a single asyncio event loop: one core is its whole
        budget, and one core is what it must be judged against. This used to
        divide by the host's core count — on a 30-core host a loop pinned at
        exactly 100% of its core read as "3%", the guard stayed green, and a
        replica sweep's upper rungs quietly measured the generator (the
        docstring above promised otherwise). psutil reports percent of one
        core, so the fraction is the reading itself.
        """
        if not self.cpu_samples:
            return 0.0
        return max(s[1] for s in self.cpu_samples)


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
            pod = response.headers.get(SERVED_BY_HEADER, "")
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


async def _raw_phase(url: str) -> str | None:
    """The job's phase, over a raw HTTP/1.1 GET — or None to use httpx.

    The phase poll is >90% of every request an async scenario makes, and
    httpx+httpcore cost ~1.6 ms of pure Python per request (pool scans,
    socket-liveness checks, connection churn) — measured pinning a generator
    process at ~600 polls/s where this path costs tens of microseconds. One
    short-lived connection per poll: the response is a few bytes of
    PlainTextResponse with a Content-Length, so Connection: close and
    read-to-EOF is the whole protocol. Anything unexpected returns None and
    that poll falls back to httpx, so correctness never rests on this path.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port or 80)
        try:
            writer.write(
                f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            await writer.drain()
            data = await asyncio.wait_for(reader.read(65536), timeout=30.0)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        head, sep, body = data.partition(b"\r\n\r\n")
        if not sep or not head.startswith(b"HTTP/1.1 200"):
            return None
        return body.decode("ascii", "replace").strip()
    except Exception:
        return None


async def _issue_async(
    client: httpx.AsyncClient,
    base_url: str,
    entry: corpus_mod.CorpusEntry,
    offered_at: float,
    recorder: Recorder,
    poll_interval: float = 0.25,
    poll_interval_max: float = 60.0,
    poll_fine_window_s: float = 5.0,
    poll_age_fraction: float = 0.1,
    timeout_s: float = 600.0,
    poll_gate: asyncio.Semaphore | None = None,
) -> None:
    """One UWS job, timed from submission to a terminal phase.

    This is the load the KEDA scenarios need: the repository's ScaledObject
    scales executors on the number of queued jobs, so only work that
    creates jobs can move it. A sync query never touches that queue.
    """
    started = time.time()
    t0 = time.perf_counter()
    status = 0
    error = ""
    ttfb = math.nan
    size = 0
    request_id = ""
    # Polls (and the result fetch) pass through the gate; the submission does
    # not, because it is the one request whose schedule the run promises to
    # keep. Waiting on a semaphore costs O(1) per waiter, where letting the
    # same excess queue inside httpcore's pool costs a rescan of the pool
    # state per event — measured at 65% of the generator's whole CPU once a
    # few hundred poll requests piled up.
    gate = poll_gate if poll_gate is not None else contextlib.nullcontext()
    pod = ""
    try:
        created = await client.post(
            f"{base_url}/tap/async",
            data={"LANG": "ADQL", "QUERY": entry.adql, "PHASE": "RUN"},
            follow_redirects=False,
        )
        status = created.status_code
        request_id = created.headers.get(REQUEST_ID_HEADER, "")
        # The pod that accepted the job, which is not necessarily the executor
        # that ran it — the point of recording it is the same routing question
        # the sync path asks.
        pod = created.headers.get(SERVED_BY_HEADER, "")
        location = created.headers.get("location", "")
        if not location:
            error = "no-job-location"
        else:
            ttfb = time.perf_counter() - t0  # the job exists from here
            deadline = t0 + timeout_s
            phase = "PENDING"
            # Fine-grained while the job is young, then backing off in
            # proportion to the job's age. Every job in flight holds one of
            # these loops, and an overloaded fleet holds thousands of jobs at
            # once: with the old fixed 2 s ceiling those loops alone were
            # thousands of requests a second — enough to pin the generator's
            # core, which is exactly what invalidated keda-K2 on
            # 20260825T220317Z (the poll storm, not the offered load). An
            # interval of a tenth of the job's age bounds every job's measured
            # completion error at ~10% of its own latency while the total poll
            # rate stays roughly (jobs in flight) / (10 x mean age) — bounded,
            # because the ages grow with the queue. The first seconds stay
            # fine-grained because that is where the SLO is decided.
            interval = poll_interval
            while time.perf_counter() < deadline:
                async with gate:
                    phase = await _raw_phase(f"{location}/phase")
                    if phase is None:
                        phase_response = await client.get(f"{location}/phase")
                        phase = phase_response.text.strip()
                if phase in ("COMPLETED", "ERROR", "ABORTED"):
                    break
                await asyncio.sleep(interval)
                age = time.perf_counter() - t0
                if age > poll_fine_window_s:
                    interval = min(max(poll_interval, age * poll_age_fraction), poll_interval_max)
            if phase == "COMPLETED":
                async with gate:
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
            pod=pod,
            mode="async",
            request_id=request_id,
        )
    )


def _client(concurrency: int, timeout_s: float, token: str | None = None) -> httpx.AsyncClient:
    # Connection limits above the offered concurrency, and keep-alive on: a
    # generator that reconnects per request measures TCP setup, and one that
    # queues internally on a small pool measures its own pool.
    limits = httpx.Limits(
        max_connections=max(concurrency * 2, 64),
        max_keepalive_connections=max(concurrency * 2, 64),
        keepalive_expiry=60.0,
    )
    # One bearer token for the whole rung, on the client rather than per
    # request. That is what a client does, and it is not a shortcut: the
    # service caches signing keys but never principals, so every request still
    # pays a full RS256 verification. Absent (the default) means the request
    # carries no Authorization header at all, which is what every other family
    # measures.
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return httpx.AsyncClient(
        limits=limits, timeout=httpx.Timeout(timeout_s), http2=False, headers=headers
    )


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
    token: str | None = None,
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

    async with _client(concurrency, timeout_s, token) as client:
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


def _closed_loop_share(payload: dict) -> tuple[list, list, float]:
    """One process's share of a sharded closed loop (runs in a child).

    Rebuilds its own workload from the picklable ingredients — each share
    gets a distinct seed, so the shares draw different query streams and
    their union is one workload rather than N copies of the same one.
    """
    if payload["query_class"] is not None:
        workload = SingleClass(payload["entries"], payload["query_class"], seed=payload["seed"])
    else:
        workload = Workload(payload["entries"], payload["mix"], seed=payload["seed"])
    recorder, elapsed = asyncio.run(
        closed_loop(
            payload["base_url"],
            workload,
            payload["concurrency"],
            payload["warmup_s"],
            payload["measure_s"],
            mode=payload["mode"],
            response_format=payload["response_format"],
            timeout_s=payload["timeout_s"],
            token=payload["token"],
        )
    )
    return recorder.samples, recorder.cpu_samples, elapsed


def closed_loop_sharded(
    base_url: str,
    *,
    entries: list,
    mix: dict | None,
    query_class: str | None,
    seed: int,
    concurrency: int,
    warmup_s: float,
    measure_s: float,
    processes: int,
    mode: str = "sync",
    response_format: str = "csv",
    timeout_s: float = 120.0,
    token: str | None = None,
) -> tuple[Recorder, float]:
    """A closed loop split across processes, so the generator scales.

    One asyncio event loop tops out around one core — roughly 400 requests
    a second of this workload — and a replica fleet worth measuring serves
    more than that. A pinned loop is the worst kind of wrong: the throughput
    curve flattens exactly as a saturated server's would, which is how an
    eight-replica sweep measured its own client. Each process runs the plain
    closed loop with an equal share of the held concurrency; the merged
    recorder's ``generator_cpu_peak`` is the busiest single process against
    its one-core budget, which is what the headroom guard judges.
    """
    processes = max(1, min(processes, concurrency))
    shares = [concurrency // processes] * processes
    for i in range(concurrency % processes):
        shares[i] += 1
    payloads = [
        {
            "base_url": base_url,
            "entries": entries,
            "mix": mix,
            "query_class": query_class,
            "seed": seed + 100 * index,
            "concurrency": share,
            "warmup_s": warmup_s,
            "measure_s": measure_s,
            "mode": mode,
            "response_format": response_format,
            "timeout_s": timeout_s,
            "token": token,
        }
        for index, share in enumerate(shares)
        if share > 0
    ]
    merged = Recorder()
    elapsed = float(measure_s)
    if len(payloads) == 1:
        samples, cpu, elapsed = _closed_loop_share(payloads[0])
        merged.samples.extend(samples)
        merged.cpu_samples.extend(cpu)
        return merged, elapsed
    # fork, not spawn: the children re-enter this module with the parent's
    # imports already made, and no asyncio loop is running in the parent at
    # this point, so there is nothing unsafe to inherit
    ctx = multiprocessing.get_context("fork")
    with concurrent.futures.ProcessPoolExecutor(len(payloads), mp_context=ctx) as pool:
        for samples, cpu, share_elapsed in pool.map(_closed_loop_share, payloads):
            merged.samples.extend(samples)
            merged.cpu_samples.extend(cpu)
            elapsed = max(elapsed, share_elapsed)
    log.info(
        "sharded closed loop c=%d over %d processes: %d requests, %.1f rps, "
        "busiest process at %.0f%% of one core",
        concurrency,
        len(payloads),
        len(merged.samples),
        len(merged.samples) / elapsed if elapsed else 0.0,
        100 * merged.generator_cpu_peak,
    )
    return merged, elapsed


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
    token: str | None = None,
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

    # Sync mode holds one streaming request per outstanding item, so its pool
    # is sized by what can be outstanding. Async mode is the opposite: a job
    # in flight holds no connection between polls, so concurrent *requests*
    # stay near (poll rate x round trip) — tens — and the pool must stay
    # small, because httpcore rescans its pool state per event: sizing it by
    # outstanding jobs let thousands of pooled connections and queued polls
    # accumulate, and that rescan was 65% of the generator's CPU. The poll
    # gate below keeps the excess in an O(1) semaphore instead; submissions
    # bypass it, so the offered schedule is never gated.
    pool = 128 if mode == "async" else min(max_in_flight, 8192)
    poll_gate = asyncio.Semaphore(64) if mode == "async" else None
    async with _client(pool, timeout_s, token) as client:
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
                        _issue_async(
                            client, base_url, entry, offered, recorder, poll_gate=poll_gate
                        )
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


def _open_loop_share(payload: dict) -> tuple[list, list, list, float]:
    """One process's share of a sharded open loop (runs in a child).

    Rebuilds its own workload from the picklable ingredients with a distinct
    seed, exactly as the closed-loop shares do, so the shares draw different
    query streams and their union is one workload rather than N copies.
    """
    if payload["query_class"] is not None:
        workload = SingleClass(payload["entries"], payload["query_class"], seed=payload["seed"])
    else:
        workload = Workload(payload["entries"], payload["mix"], seed=payload["seed"])
    steps = [Step(**s) for s in payload["steps"]]
    started = time.time()
    recorder, timeline = asyncio.run(
        open_loop(
            payload["base_url"],
            workload,
            steps,
            mode=payload["mode"],
            response_format=payload["response_format"],
            arrival_seed=payload["arrival_seed"],
            max_in_flight=payload["max_in_flight"],
            token=payload["token"],
        )
    )
    return recorder.samples, recorder.cpu_samples, timeline, time.time() - started


def open_loop_sharded(
    base_url: str,
    *,
    entries: list,
    mix: dict | None,
    query_class: str | None,
    seed: int,
    steps: list[Step],
    processes: int,
    mode: str = "sync",
    response_format: str = "csv",
    arrival_seed: int = 90210,
    max_in_flight: int = 4096,
    token: str | None = None,
) -> tuple[Recorder, list[dict], float]:
    """An open loop split across processes, so the generator scales.

    The async scenarios need this even at modest offered rates: every job in
    flight holds a phase-poll loop, so a scenario that queues thousands of
    jobs makes the generator's real request rate the poll rate, not the
    arrival rate — one asyncio loop pinned its core exactly that way while
    offering 24 jobs/s. Each process runs the plain open loop with an equal
    share of every step's rate; the sum of N thinned Poisson processes at
    rate/N is the same Poisson process at the full rate, so the offered load
    is statistically unchanged. Distinct arrival seeds keep the shards from
    submitting in lockstep, and the in-flight cap is split so the total
    outstanding work stays what the config states.
    """
    processes = max(1, processes)
    payloads = [
        {
            "base_url": base_url,
            "entries": entries,
            "mix": mix,
            "query_class": query_class,
            "seed": seed + 100 * index,
            "steps": [
                dataclasses.asdict(s)
                | {
                    "rate": s.rate / processes,
                    "rate_end": None if s.rate_end is None else s.rate_end / processes,
                }
                for s in steps
            ],
            "mode": mode,
            "response_format": response_format,
            "arrival_seed": arrival_seed + 137 * index,
            "max_in_flight": max(1, max_in_flight // processes),
            "token": token,
        }
        for index in range(processes)
    ]
    merged = Recorder()
    merged_timeline: list[dict] = []
    elapsed = 0.0
    if len(payloads) == 1:
        # one shard: run it here rather than forking a pool to hold it, the
        # same short-circuit the closed loop takes
        samples, cpu, timeline, elapsed = _open_loop_share(payloads[0])
        merged.samples.extend(samples)
        merged.cpu_samples.extend(cpu)
        return merged, timeline, elapsed
    # fork, not spawn, for the same reason as the closed-loop shards: the
    # children re-enter this module with the parent's imports already made,
    # and no asyncio loop is running in the parent at this point
    ctx = multiprocessing.get_context("fork")
    with concurrent.futures.ProcessPoolExecutor(len(payloads), mp_context=ctx) as pool:
        for samples, cpu, timeline, share_elapsed in pool.map(_open_loop_share, payloads):
            merged.samples.extend(samples)
            merged.cpu_samples.extend(cpu)
            merged_timeline.extend(timeline)
            elapsed = max(elapsed, share_elapsed)
    log.info(
        "sharded open loop over %d processes: %d requests, busiest process at %.0f%% of one core",
        len(payloads),
        len(merged.samples),
        100 * merged.generator_cpu_peak,
    )
    return merged, merged_timeline, elapsed


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
