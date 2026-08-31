"""Closed-loop load generation against any TAP service.

N clients, each issuing the next request when the last returns: this finds a
service's capacity and its latency under a known parallelism, and — unlike an
open loop — cannot overload anything, which is the right property for a
harness pointed at servers we do not own the internals of.

Every request is recorded individually. Aggregates are computed later, from
the samples on disk, so a percentile can be recomputed without re-running
anything and a suspicious summary can always be taken back to the requests it
came from.

``base_url`` is the TAP root (e.g. ``http://localhost:8080/tap``): the sync
endpoint is ``{base_url}/sync`` and the UWS tree ``{base_url}/async``, which
is what TAP 1.1 prescribes and every compared server serves.
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

from . import corpus as corpus_mod

log = logging.getLogger("tap_compare.runner")

# egernia returns these; other servers do not, and an absent header simply
# records as "" — the analysis treats them as optional.
REQUEST_ID_HEADER = "X-Request-ID"
SERVED_BY_HEADER = "X-Served-By"


@dataclasses.dataclass(slots=True)
class Sample:
    """One request. The unit of everything downstream."""

    t_start: float  # unix seconds, when the request was issued
    t_offered: float  # when it *should* have been issued (open loop; == t_start here)
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

    def add(self, sample: Sample) -> None:
        self.samples.append(sample)

    async def watch_self(self, stop: asyncio.Event, interval: float = 2.0) -> None:
        self.process.cpu_percent(None)  # prime the counter
        while not stop.is_set():
            await asyncio.sleep(interval)
            # Per-core fraction for the process, and the whole host, so a
            # generator that is fine but a host that is not can be told apart.
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
        budget, and one core is what it must be judged against. psutil
        reports percent of one core, so the fraction is the reading itself.
        """
        if not self.cpu_samples:
            return 0.0
        return max(s[1] for s in self.cpu_samples)


class Workload:
    """Draws queries from the corpus according to the mix.

    Deterministic: the same seed issues the same sequence of queries in the
    same order, so two runs — and two *servers* — differ in what the service
    did rather than in what it was asked to do.
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
    """One query class only, for the per-class rungs."""

    def __init__(self, entries: list[corpus_mod.CorpusEntry], query_class: str, seed: int) -> None:
        super().__init__(entries, {query_class: 1.0}, seed)


async def _issue_sync(
    client: httpx.AsyncClient,
    base_url: str,
    entry: corpus_mod.CorpusEntry,
    offered_at: float,
    recorder: Recorder,
    response_format: str,
    maxrec: int | None,
) -> None:
    """One {base}/sync request, timed to the last byte.

    MAXREC is sent explicitly when configured: the compared servers have
    different defaults, and leaving it to the server would silently compare
    different row counts.
    """
    started = time.time()
    t0 = time.perf_counter()
    ttfb = math.nan
    size = 0
    status = 0
    error = ""
    pod = ""
    request_id = ""
    data = {"LANG": "ADQL", "QUERY": entry.adql, "RESPONSEFORMAT": response_format}
    if maxrec is not None:
        data["MAXREC"] = str(maxrec)
    try:
        async with client.stream("POST", f"{base_url}/sync", data=data) as response:
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

    The phase poll is >90% of every request an async rung makes, and
    httpx+httpcore cost ~1.6 ms of pure Python per request. One short-lived
    connection per poll: the response is a few bytes with a Content-Length,
    so Connection: close and read-to-EOF is the whole protocol. Anything
    unexpected returns None and that poll falls back to httpx, so
    correctness never rests on this path.
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
) -> None:
    """One UWS job, timed from submission to a terminal phase."""
    started = time.time()
    t0 = time.perf_counter()
    status = 0
    error = ""
    ttfb = math.nan
    size = 0
    request_id = ""
    pod = ""
    try:
        created = await client.post(
            f"{base_url}/async",
            data={"LANG": "ADQL", "QUERY": entry.adql, "PHASE": "RUN"},
            follow_redirects=False,
        )
        status = created.status_code
        request_id = created.headers.get(REQUEST_ID_HEADER, "")
        pod = created.headers.get(SERVED_BY_HEADER, "")
        location = created.headers.get("location", "")
        if not location:
            error = "no-job-location"
        else:
            ttfb = time.perf_counter() - t0  # the job exists from here
            deadline = t0 + timeout_s
            phase = "PENDING"
            # Fine-grained while the job is young, then backing off in
            # proportion to the job's age, so the poll rate stays bounded
            # however deep the queue behind it is.
            interval = poll_interval
            while time.perf_counter() < deadline:
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


def _client(concurrency: int, timeout_s: float) -> httpx.AsyncClient:
    # Connection limits above the offered concurrency, and keep-alive on: a
    # generator that reconnects per request measures TCP setup, and one that
    # queues internally on a small pool measures its own pool. The same
    # client shape is used against every server — connection handling is
    # part of what is being held equal.
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
    maxrec: int | None = None,
    timeout_s: float = 120.0,
) -> tuple[Recorder, float]:
    """N clients, each issuing the next request as soon as the last finishes.

    Returns the recorder and how long the measured phase actually took. Those
    are not the same as the requested duration: a worker only checks the clock
    between requests, and a streaming response can drip for minutes, so a
    phase can overrun its window substantially. Dividing the request count by
    the *requested* duration would then overstate throughput by whatever the
    overrun was.
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
                    await _issue_sync(client, base_url, entry, now, target, response_format, maxrec)
                else:
                    await _issue_async(client, base_url, entry, now, target)

        if warmup_s > 0:
            # Cancelled at the deadline rather than waited out. The warmup's
            # results are discarded anyway, and a single slow-streaming
            # response would otherwise hold the whole run for as long as it
            # keeps trickling.
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
            maxrec=payload["maxrec"],
            timeout_s=payload["timeout_s"],
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
    maxrec: int | None = None,
    timeout_s: float = 120.0,
) -> tuple[Recorder, float]:
    """A closed loop split across processes, so the generator scales.

    One asyncio event loop tops out around one core — roughly 400 requests
    a second of this workload — and a server worth measuring can serve more
    than that. A pinned loop is the worst kind of wrong: the throughput
    curve flattens exactly as a saturated server's would. Each process runs
    the plain closed loop with an equal share of the held concurrency; the
    merged recorder's ``generator_cpu_peak`` is the busiest single process
    against its one-core budget, which is what the headroom guard judges.
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
            "maxrec": maxrec,
            "timeout_s": timeout_s,
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
