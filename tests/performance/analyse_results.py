"""Turn raw scale-test artifacts into a concise bottleneck report."""

import argparse
import csv
import json
import re
from pathlib import Path


def _number(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, 0) or 0)
    except ValueError:
        return 0.0


def _client_count(path: Path) -> int:
    match = re.search(r"-c(\\d+)", path.stem)
    return int(match.group(1)) if match else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    args = parser.parse_args()

    reports = []
    for path in sorted(args.results.glob("tap-c*.json"), key=_client_count):
        reports.append(json.loads(path.read_text()))

    lines = [
        "# Performance bottleneck summary",
        "",
        "## End-to-end TAP scaling",
        "",
        "| Clients | Requests/s | p50 (ms) | p95 (ms) | p99 (ms) | Error rate |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        latency = report["latency"]
        lines.append(
            f"| {report['clients']} | {report['requests_per_second']:.2f} "
            f"| {latency['p50_seconds'] * 1000:.1f} "
            f"| {latency['p95_seconds'] * 1000:.1f} "
            f"| {latency['p99_seconds'] * 1000:.1f} "
            f"| {report['error_rate']:.2%} |"
        )

    findings = []
    for previous, current in zip(reports, reports[1:], strict=False):
        throughput_growth = current["requests_per_second"] / max(
            previous["requests_per_second"], 0.000001
        )
        latency_growth = current["latency"]["p95_seconds"] / max(
            previous["latency"]["p95_seconds"], 0.000001
        )
        if throughput_growth < 1.20 and latency_growth > 1.50:
            findings.append(
                "Likely saturation between "
                f"{previous['clients']} and {current['clients']} clients: "
                f"throughput changed {throughput_growth:.2f}x while p95 latency "
                f"changed {latency_growth:.2f}x. Check the connection-pool wait "
                "frames in the Python flamegraph and PostgreSQL wait events."
            )
        if current["error_rate"] > 0:
            findings.append(
                f"{current['clients']} clients produced an error rate of "
                f"{current['error_rate']:.2%}; inspect tap-api.log and PostgreSQL logs."
            )

    stat_files = sorted(args.results.glob("pgstat-tap-c*.csv"), key=_client_count)
    if stat_files:
        highest = stat_files[-1]
        with highest.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows.sort(key=lambda row: _number(row, "total_exec_time"), reverse=True)
        lines.extend(
            [
                "",
                f"## Most expensive SQL at {_client_count(highest)} clients",
                "",
                "| Calls | Total (ms) | Mean (ms) | Cache hit | Temp blocks | Query |",
                "|---:|---:|---:|---:|---:|---|",
            ]
        )
        for row in rows[:5]:
            hits = _number(row, "shared_blks_hit")
            reads = _number(row, "shared_blks_read")
            hit_ratio = hits / (hits + reads) if hits + reads else 1.0
            temp = _number(row, "temp_blks_read") + _number(row, "temp_blks_written")
            query = " ".join((row.get("query") or "").split()).replace("|", "\\|")
            lines.append(
                f"| {int(_number(row, 'calls'))} "
                f"| {_number(row, 'total_exec_time'):.1f} "
                f"| {_number(row, 'mean_exec_time'):.2f} "
                f"| {hit_ratio:.1%} | {int(temp)} | {query[:100]} |"
            )
            if temp:
                findings.append(
                    "The highest-concurrency workload spilled temporary blocks. "
                    "Inspect sort/hash nodes in cone-plan.json and review work_mem "
                    "only after confirming the responsible plan."
                )
            if hits + reads and hit_ratio < 0.90:
                findings.append(
                    f"A top query had a {hit_ratio:.1%} shared-buffer hit ratio. "
                    "Compare working-set size with shared_buffers and storage latency."
                )

    lines.extend(["", "## Automated findings", ""])
    if findings:
        lines.extend(f"- {finding}" for finding in dict.fromkeys(findings))
    else:
        lines.append(
            "- No simple saturation, error, temporary-spill, or low-cache-hit rule fired. "
            "Use the attached flamegraph and JSON execution plan for deeper inspection."
        )
    lines.extend(
        [
            "",
            "## Where to look next",
            "",
            "- Open tap-python-flamegraph.svg; wide application frames are the Python "
            "functions consuming the most sampled time.",
            "- Open cone-plan.json; compare estimated and actual rows and inspect buffer "
            "reads around scan, join, sort, and aggregate nodes.",
            "- Use the per-concurrency pgstat CSV files to distinguish a slow query "
            "from connection-pool or application saturation.",
            "",
        ]
    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
