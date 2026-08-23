"""Concurrent TAP /sync workload used by the scheduled performance workflow."""

import argparse
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


def _worker(
    worker_id: int,
    base_url: str,
    deadline: float,
    scale_rows: int,
    timeout: float,
) -> list[dict]:
    rng = random.Random(worker_id)
    results = []
    with httpx.Client(timeout=timeout) as client:
        while time.monotonic() < deadline:
            name, template = rng.choices(WORKLOAD, weights=WEIGHTS, k=1)[0]
            query = template.format(
                source_id=rng.randint(1, scale_rows),
                ra=rng.uniform(0.0, 360.0),
                dec=rng.uniform(-80.0, 80.0),
            )
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
                status = 0
                size = 0
            results.append(
                {
                    "workload": name,
                    "seconds": time.perf_counter() - started,
                    "ok": ok,
                    "status": status,
                    "bytes": size,
                }
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/tap")
    parser.add_argument("--clients", type=int, required=True)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--scale-rows", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.clients < 1 or args.duration < 1 or args.scale_rows < 1:
        parser.error("clients, duration, and scale-rows must be positive")

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
            )
            for worker_id in range(args.clients)
        ]
    samples = [sample for future in futures for sample in future.result()]
    latencies = [sample["seconds"] for sample in samples]
    successful = sum(sample["ok"] for sample in samples)
    # Wall clock from submit to the last future, not max(duration, slowest
    # request): a worker that starts a request just before the deadline
    # finishes after it, and charging those rows to `duration` inflates
    # throughput exactly when the service is slowest.
    elapsed = max(time.monotonic() - started, 1e-9)
    by_workload = {}
    for name, _query in WORKLOAD:
        subset = [sample["seconds"] for sample in samples if sample["workload"] == name]
        by_workload[name] = {"requests": len(subset), **_percentiles(subset)}

    report = {
        "clients": args.clients,
        "duration_seconds": args.duration,
        "elapsed_seconds": round(elapsed, 6),
        "scale_rows": args.scale_rows,
        "requests": len(samples),
        "successful": successful,
        "errors": len(samples) - successful,
        "error_rate": (len(samples) - successful) / len(samples) if samples else 1.0,
        "requests_per_second": successful / elapsed,
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
