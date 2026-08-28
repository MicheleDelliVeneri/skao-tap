"""Concurrent TAP /sync workload used by the scheduled performance workflow."""

import argparse
import collections
import concurrent.futures
import json
import math
import random
import statistics
import threading
import time
from pathlib import Path

import httpx

WORKLOAD = (
    ("metadata", "SELECT TOP 100 table_name FROM tap_schema.tables"),
    ("point", "SELECT source_id, ra, dec, flux FROM perf.sources WHERE source_id = {source_id}"),
    (
        "cone",
        "SELECT TOP 100 source_id, ra, dec, flux FROM perf.sources "
        "WHERE 1=CONTAINS(POINT('ICRS', ra, dec), "
        "CIRCLE('ICRS', {ra}, {dec}, 0.5))",
    ),
    ("stream", "SELECT TOP 1000 source_id, ra, dec, flux FROM perf.sources"),
)
WEIGHTS = (1, 6, 2, 1)


def _percentiles(values: list[float]) -> dict[str, float]:
    """p50/p95/p99 from a single sort.

    All three come from one ordering rather than three: the sorting happens
    after the run is measured, so it never skewed a number, but sorting the
    same list three times to read three indices out of it was work for
    nothing.

    0.0 for an empty input, not NaN: the report is written with
    allow_nan=False, so a workload that recorded nothing would raise instead
    of reporting. The sibling "requests" count is what says it was empty.
    """
    if not values:
        return {"p50_seconds": 0.0, "p95_seconds": 0.0, "p99_seconds": 0.0}
    ordered = sorted(values)

    def at(percentile: float) -> float:
        index = min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1)
        return ordered[index]

    return {
        "p50_seconds": at(0.50),
        "p95_seconds": at(0.95),
        "p99_seconds": at(0.99),
    }


def _parameter_pool(scale_rows: int, size: int) -> tuple[tuple[int, float, float], ...]:
    """A fixed set of query parameters, so the query *texts* repeat.

    ADQL has no bind parameters: the text is the whole input, so a workload
    that randomises a cone centre per request is 0% repetitive by design --
    which is what this harness has always been, deliberately, so a run measured
    the service rather than a warm cache.

    That is now one of two things worth measuring rather than the only one.
    `translate()` memoises (#107), and the cache's value is linear in the hit
    rate, which nobody can observe on traffic we generate. So the harness gets
    a knob instead of an opinion: size 0 keeps the original behaviour and
    measures the service at a hit rate of ~0 -- the case that has to show *no
    regression*, because it is where the cache is pure overhead -- and a small
    size drives the hit rate to ~1 and measures the ceiling. The real answer is
    between them, at whatever hit rate `tap_adql_translation_cache_hits_total`
    reports in the field.

    Seeded independently of the workers so every worker draws from the same
    pool: the cache is per process, so texts have to repeat *across* clients to
    be repetitive at the service.
    """
    rng = random.Random(20260828)
    return tuple(
        (
            rng.randint(1, scale_rows),
            rng.uniform(0.0, 360.0),
            rng.uniform(-80.0, 80.0),
        )
        for _ in range(size)
    )


def _worker(
    worker_id: int,
    base_url: str,
    deadline: float,
    scale_rows: int,
    timeout: float,
    pool: tuple[tuple[int, float, float], ...] = (),
) -> dict:
    rng = random.Random(worker_id)
    # Aggregated in the worker rather than one dict per request: the
    # percentiles need every successful latency, so those are kept as plain
    # floats, and everything else collapses into counters. A status histogram
    # costs nothing to keep and is the first thing worth knowing when the
    # error rate is not zero.
    attempted: collections.Counter[str] = collections.Counter()
    latencies: dict[str, list[float]] = {name: [] for name, _ in WORKLOAD}
    statuses: collections.Counter[int] = collections.Counter()
    bytes_read = 0
    with httpx.Client(timeout=timeout) as client:
        while time.monotonic() < deadline:
            # The template is always drawn from the weighted mix, so the
            # workload's shape is identical either way; only the diversity of
            # the parameters -- and so of the query texts -- changes.
            name, template = rng.choices(WORKLOAD, weights=WEIGHTS, k=1)[0]
            source_id, ra, dec = (
                rng.choice(pool)
                if pool
                else (
                    rng.randint(1, scale_rows),
                    rng.uniform(0.0, 360.0),
                    rng.uniform(-80.0, 80.0),
                )
            )
            query = template.format(source_id=source_id, ra=ra, dec=dec)
            started = time.perf_counter()
            try:
                response = client.post(
                    f"{base_url.rstrip('/')}/sync",
                    data={"LANG": "ADQL", "QUERY": query, "FORMAT": "json"},
                )
                response.read()
                ok = response.status_code == 200
                status = response.status_code
                size = len(response.content)
            except httpx.HTTPError:
                ok = False
                status = 0  # no response: a connect error, or the timeout
                size = 0
            elapsed = time.perf_counter() - started
            attempted[name] += 1
            statuses[status] += 1
            bytes_read += size
            if ok:
                latencies[name].append(elapsed)
    return {
        "attempted": attempted,
        "latencies": latencies,
        "statuses": statuses,
        "bytes_read": bytes_read,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/tap")
    parser.add_argument("--clients", type=int, required=True)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--scale-rows", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--distinct-params",
        type=int,
        default=0,
        help="draw parameters from a fixed pool of this size so query texts"
        " repeat; 0 (default) randomises every request, which is a translation"
        " cache hit rate of ~0",
    )
    args = parser.parse_args()
    if args.clients < 1 or args.duration < 1 or args.scale_rows < 1:
        parser.error("clients, duration, and scale-rows must be positive")
    if args.distinct_params < 0:
        parser.error("distinct-params must not be negative")
    pool = _parameter_pool(args.scale_rows, args.distinct_params)

    started = time.monotonic()
    deadline = started + args.duration
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.clients,
        thread_name_prefix="tap-load",
    ) as executor:
        futures = [
            executor.submit(
                _worker,
                worker_id,
                args.base_url,
                deadline,
                args.scale_rows,
                args.timeout,
                pool,
            )
            for worker_id in range(args.clients)
        ]
    attempted: collections.Counter[str] = collections.Counter()
    statuses: collections.Counter[int] = collections.Counter()
    by_name: dict[str, list[float]] = {name: [] for name, _ in WORKLOAD}
    bytes_read = 0
    for future in futures:
        result = future.result()
        attempted.update(result["attempted"])
        statuses.update(result["statuses"])
        bytes_read += result["bytes_read"]
        for name, values in result["latencies"].items():
            by_name[name].extend(values)

    # Latency covers successful requests only. A request that failed fast — a
    # 500, or a timeout raising early — would otherwise pull p95 and p99 down,
    # so the report would read best exactly when the service is worst. The
    # error counts carry the failures instead.
    latencies = [value for values in by_name.values() for value in values]
    requests = sum(attempted.values())
    successful = len(latencies)
    # Wall clock from submit to the last future, not max(duration, slowest
    # request): a worker that starts a request just before the deadline
    # finishes after it, and charging those rows to `duration` inflates
    # throughput exactly when the service is slowest.
    elapsed = max(time.monotonic() - started, 1e-9)
    by_workload = {
        name: {
            "requests": attempted[name],
            "successful": len(by_name[name]),
            **_percentiles(by_name[name]),
        }
        for name, _query in WORKLOAD
    }

    report = {
        "clients": args.clients,
        "duration_seconds": args.duration,
        "elapsed_seconds": round(elapsed, 6),
        "scale_rows": args.scale_rows,
        # 0 means every request had its own query text. Recorded because the
        # translation cache's hit rate follows from it, so a report without it
        # cannot be compared with another.
        "distinct_params": args.distinct_params,
        "requests": requests,
        "successful": successful,
        "errors": requests - successful,
        "error_rate": (requests - successful) / requests if requests else 1.0,
        "requests_per_second": successful / elapsed,
        "bytes_per_second": bytes_read / elapsed,
        # 0 means no response at all — a connect error or the client timeout
        "status_counts": {str(code): count for code, count in sorted(statuses.items())},
        "latency": {
            "mean_seconds": statistics.fmean(latencies) if latencies else 0.0,
            **_percentiles(latencies),
        },
        "by_workload": by_workload,
        "python_threads_at_completion": threading.active_count(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["error_rate"] <= 0.01 else 1


if __name__ == "__main__":
    raise SystemExit(main())
