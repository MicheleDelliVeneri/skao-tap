"""Render a comparison run into a docs/performance report.

The report carries its own honesty machinery: gate outcomes sit above the
numbers, every comparative cell has a confidence interval, the tie rule is
applied mechanically, and the claims section says what the run may and may
not support. A report where the reader has to trust the author is a report;
one where the reader can recompute is evidence.
"""

from __future__ import annotations

import csv
import json
import pathlib
import shutil

from . import stats

#: the pre-registered tie rule: a comparison is a tie when the 95% intervals
#: overlap, or the throughput means are under this fraction apart
RPS_FLOOR = 0.10
#: a target erroring beyond this fraction of requests cannot win a cell
#: (its error responses inflate its throughput); both targets erroring, or
#: a tripped generator guard, voids the cell's verdict entirely
ERROR_CEILING = 0.01

CSV_COLUMNS = [
    "target",
    "server",
    "query_class",
    "response_format",
    "concurrency",
    "repetition",
    "requests",
    "error_fraction",
    "rps",
    "latency_p50_s",
    "latency_p95_s",
    "ttfb_p95_s",
    "mean_response_bytes",
    "generator_cpu_peak",
    "generator_guard_ok",
]


def _flat(row: dict) -> dict:
    return {
        "target": row["target"],
        "server": row["server"],
        "query_class": row["query_class"],
        "response_format": row["response_format"],
        "concurrency": row["concurrency"],
        "repetition": row["repetition"],
        "requests": row["requests"],
        "error_fraction": round(row["error_fraction"], 6),
        "rps": round(row["rps"], 3),
        "latency_p50_s": round(row["latency"]["p50_s"], 6),
        "latency_p95_s": round(row["latency"]["p95_s"], 6),
        "ttfb_p95_s": round(row["ttfb"]["p95_s"], 6),
        "mean_response_bytes": round(row["mean_response_bytes"], 1),
        "generator_cpu_peak": round(row["generator_cpu_peak"], 3),
        "generator_guard_ok": row["generator_guard_ok"],
    }


def aggregate(rows: list[dict]) -> dict:
    """Per (class, format, concurrency, target): mean and CI across reps."""
    cells: dict[tuple, dict] = {}
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["query_class"], row["response_format"], row["concurrency"], row["target"])
        grouped.setdefault(key, []).append(row)
    for key, reps in grouped.items():
        cells[key] = {
            "rps": stats.mean_ci([r["rps"] for r in reps]),
            "p95": stats.mean_ci([r["latency"]["p95_s"] for r in reps]),
            "errors": max(r["error_fraction"] for r in reps),
            "guard_ok": all(r["generator_guard_ok"] for r in reps),
            "reps": len(reps),
        }
    return cells


def _overlap(a: dict, b: dict) -> bool:
    """Whether two mean_ci results overlap (or lack intervals to compare)."""
    if a["ci95_low"] is None or b["ci95_low"] is None:
        return True  # one measurement has no spread: never claim a difference
    return a["ci95_low"] <= b["ci95_high"] and b["ci95_low"] <= a["ci95_high"]


def verdict(cells: dict, key_a: tuple, key_b: tuple) -> str:
    """Return "tie", "invalid", or the winning target's name, by the pre-registered rule.

    A tripped generator guard invalidates the cell: the harness measured
    itself. A target whose requests errored beyond the ceiling cannot *win* —
    error responses return fast and inflate its throughput — but a clean
    opponent still can: the errors are the server's own behaviour under that
    load, and the page prints them beside the number.
    """
    a, b = cells[key_a], cells[key_b]
    if not (a["guard_ok"] and b["guard_ok"]):
        return "invalid"
    a_clean = a["errors"] <= ERROR_CEILING
    b_clean = b["errors"] <= ERROR_CEILING
    if not (a_clean or b_clean):
        return "invalid"
    if a_clean != b_clean:
        return (key_a if a_clean else key_b)[3]
    rps_a, rps_b = a["rps"], b["rps"]
    if rps_a["mean"] is None or rps_b["mean"] is None:
        return "tie"
    hi, lo = (a, b) if rps_a["mean"] >= rps_b["mean"] else (b, a)
    hi_key = key_a if hi is a else key_b
    relative = (hi["rps"]["mean"] - lo["rps"]["mean"]) / max(lo["rps"]["mean"], 1e-9)
    if _overlap(hi["rps"], lo["rps"]) or relative < RPS_FLOOR:
        return "tie"
    return hi_key[3]  # the target name


def _fmt(ci: dict, digits: int = 1) -> str:
    if ci["mean"] is None:
        return "—"
    if ci["ci95_low"] is None:
        return f"{ci['mean']:.{digits}f}"
    half = (ci["ci95_high"] - ci["ci95_low"]) / 2
    return f"{ci['mean']:.{digits}f} ±{half:.{digits}f}"


PARITY_INTRO = [
    "Same-hardware TAP-server comparison: identical logical corpus, each",
    "server deployed per its own documentation, one target under load at",
    "a time (all stacks stay up so repetitions interleave), the identical",
    "seeded query stream, MAXREC pinned on every request. Every target",
    "stack is pinned to the same 8 CPU / 8 GiB",
    "budget: DaCHS in `benchmarks/tap-compare/docker-compose.dachs.yml`",
    "(`cpus: 8`, `mem_limit: 8g`), egernia in",
    "`benchmarks/tap-compare/docker-compose.egernia-pins.yml` (shared",
    "`cpuset` of 8 cores; 8 GiB split 4 db / 2 api / 2 executor).",
    "See `benchmarks/tap-compare/README.md` for the protocol.",
]
SCALING_INTRO = [
    "Same-hardware TAP-server resource-scaling comparison: the parity",
    "protocol's corpus, gates, query stream, formats and statistics, with",
    "both servers' resource pins raised tier by tier. Within a tier each",
    "server is measured alone under that tier's pins (the host cannot hold",
    "two pinned stacks of the larger tiers at once), so repetitions do not",
    "interleave across servers; the gates run per tier with both stacks up.",
    "The pins actually applied, as `docker inspect` and `SHOW` saw them, are",
    "under `pins/`. See `benchmarks/tap-compare/scaling/PROTOCOL.md` for",
    "the pre-registered design. Where `resources.jsonl` covers a rung, the",
    "resource tables give each server's CPU cores used, CPU time per request",
    "and memory over the rung's window (`resources.csv` has every rung);",
    "rungs measured before the sampler started show `—`.",
]


#: which sampled containers make up each server (scaling/sample_resources.sh)
SERVER_CONTAINERS = {
    "egernia": ("egernia-db-1", "egernia-tap-api-1", "egernia-tap-executor-1"),
    "dachs": ("tap-compare-dachs-1",),
}
API_CONTAINER = "egernia-tap-api-1"
#: a rung whose window the samples cover less than this is reported without resources
MIN_COVERAGE = 0.8  # 5 s samples inside a 60 s window cover at worst 50 s of it
RESOURCE_COLUMNS = [
    "target",
    "tier",
    "query_class",
    "response_format",
    "concurrency",
    "repetition",
    "requests",
    "rps",
    "window_seconds",
    "coverage",
    "cpu_seconds",
    "cpu_cores",
    "cpu_seconds_per_request",
    "mem_mean_bytes",
    "mem_peak_bytes",
    "api_workers",
]


def _rung_key(row: dict) -> str:
    prefix = f"t{row['tier']}-" if row.get("tier") else ""
    return (
        f"{prefix}{row['target']}-{row['response_format']}-{row['query_class']}"
        f"-c{row['concurrency']}-r{row['repetition']}"
    )


def _window(parquet: pathlib.Path) -> tuple[float, float]:
    """The rung's measured window from its own request samples."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    table = pq.read_table(parquet, columns=["t_start", "latency_s"])
    ends = pc.add(table["t_start"], table["latency_s"])
    return pc.min(table["t_start"]).as_py(), pc.max(ends).as_py()


def resources(run_dir: pathlib.Path, rows: list[dict]) -> dict[str, dict]:
    """Per rung: CPU-seconds, cores, CPU per request, memory, API workers.

    Joins scaling/sample_resources.sh's ``resources.jsonl`` (cgroup counters
    every few seconds) to each rung's measured window. CPU is the counter
    difference between the first and last sample inside the window, scaled
    to the window; memory is the mean and peak of the samples inside it. A
    server is the sum of its containers. Rungs the sampler did not cover
    (started later, or a gap) are absent from the result, never guessed.
    """
    path = run_dir / "resources.jsonl"
    if not path.exists():
        return {}
    samples: dict[str, list[tuple[float, int, int, int]]] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        samples.setdefault(rec["container"], []).append(
            (rec["t"], rec["cpu_usec"], rec["mem_bytes"], rec["python_procs"])
        )
    for series in samples.values():
        series.sort()  # two writers, or a restart, must not disorder a window
    out: dict[str, dict] = {}
    for row in rows:
        key = _rung_key(row)
        parquet = run_dir / "samples" / f"{key}.parquet"
        if not parquet.exists() or not row["requests"]:
            continue
        containers = SERVER_CONTAINERS.get(row["server"])
        if not containers:
            continue  # a server the sampler does not know: not covered, not zero
        start, end = _window(parquet)
        span = end - start
        if span <= 0:
            continue  # a degenerate window has no rate to report
        inside = {c: [s for s in samples.get(c, []) if start <= s[0] <= end] for c in containers}
        spans = {c: v[-1][0] - v[0][0] if len(v) >= 2 else 0.0 for c, v in inside.items()}
        if any(s <= 0 for s in spans.values()):
            continue  # too few samples, or duplicate timestamps: not covered
        coverage = min(spans.values()) / span
        cpu_seconds = sum((v[-1][1] - v[0][1]) / 1e6 * span / spans[c] for c, v in inside.items())
        # the server's memory is the containers summed per sampling tick (the
        # sampler stamps one tick's lines with one timestamp; ticks are
        # aligned to the second), and its peak is the peak of that series —
        # container peaks at different moments must not add up
        per_tick: dict[int, dict[str, int]] = {}
        for container, series in inside.items():
            for t, _cpu, mem, _procs in series:
                per_tick.setdefault(round(t), {})[container] = mem
        totals = [sum(m.values()) for m in per_tick.values() if len(m) == len(containers)]
        if not totals:
            continue
        workers = None
        if API_CONTAINER in inside:
            procs = max(s[3] for s in inside[API_CONTAINER])
            workers = 1 if procs <= 1 else procs - 1  # a supervisor above one worker
        mem_mean = sum(totals) / len(totals)
        mem_peak = max(totals)
        if coverage < MIN_COVERAGE:
            continue
        out[key] = {
            "window_seconds": span,
            "coverage": coverage,
            "cpu_seconds": cpu_seconds,
            "cpu_cores": cpu_seconds / span,
            "cpu_seconds_per_request": cpu_seconds / row["requests"],
            "mem_mean_bytes": mem_mean,
            "mem_peak_bytes": mem_peak,
            "api_workers": workers,
        }
    return out


def _gib(value: float) -> str:
    return f"{value / 2**30:.2f}"


def _resource_tables(
    rows: list[dict], usage: dict[str, dict], targets: list[str], heading: str
) -> list[str]:
    """Per format: cores, CPU-s per request and memory per cell; then the
    mixed workload's throughput against cores used, for plotting."""
    lines: list[str] = []
    by_cell: dict[tuple, list[dict]] = {}
    for row in rows:
        res = usage.get(_rung_key(row))
        if res:
            by_cell.setdefault(
                (row["query_class"], row["response_format"], row["concurrency"], row["target"]),
                [],
            ).append({**res, "rps": row["rps"]})
    if not by_cell:
        return lines

    def cell(cls, fmt, conc, target) -> dict | None:
        reps = by_cell.get((cls, fmt, conc, target))
        if not reps:
            return None
        n = len(reps)
        return {
            k: sum(r[k] for r in reps) / n
            for k in ("cpu_cores", "cpu_seconds_per_request", "mem_mean_bytes", "rps")
        } | {
            "mem_peak_bytes": max(r["mem_peak_bytes"] for r in reps),
            "api_workers": reps[0]["api_workers"],
            "reps": n,
        }

    formats = sorted({k[1] for k in by_cell})
    classes = sorted({k[0] for k in by_cell})
    concurrencies = sorted({k[2] for k in by_cell})
    for fmt in formats:
        lines += ["", f"{heading} resources, {fmt}", ""]
        header, rule = "| class | c |", "| --- | --- |"
        for name in targets:
            header += f" {name} cores | CPU s/req | mem mean GiB | mem peak GiB |"
            rule += " --- | --- | --- | --- |"
        lines += [header, rule]
        for cls in classes:
            for conc in concurrencies:
                cells = [cell(cls, fmt, conc, t) for t in targets]
                if not any(cells):
                    continue
                line = f"| {cls} | {conc} |"
                for c in cells:
                    if c is None:
                        line += " — | — | — | — |"
                    else:
                        line += (
                            f" {c['cpu_cores']:.2f} | {c['cpu_seconds_per_request'] * 1e3:.1f} ms |"
                            f" {_gib(c['mem_mean_bytes'])} | {_gib(c['mem_peak_bytes'])} |"
                        )
                lines.append(line)
    lines += ["", f"{heading} throughput vs CPU cores used (mix)", ""]
    lines += [
        "| format | c | target | API workers | rps | cores | rps per core | CPU ms/req |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for fmt in formats:
        for conc in concurrencies:
            for name in targets:
                c = cell("mix", fmt, conc, name)
                if c is None:
                    continue
                per_core = c["rps"] / c["cpu_cores"] if c["cpu_cores"] else 0.0
                lines.append(
                    f"| {fmt} | {conc} | {name} | {c['api_workers'] or '—'} | {c['rps']:.1f} |"
                    f" {c['cpu_cores']:.2f} | {per_core:.1f} |"
                    f" {c['cpu_seconds_per_request'] * 1e3:.1f} |"
                )
    return lines


def _tier_key(tier: str | None) -> tuple:
    return (0, int(tier)) if tier and tier.isdigit() else (1, tier or "")


def _gates_section(gates: dict, targets: list[str], heading: str) -> list[str]:
    lines = [
        "",
        f"{heading} Gates",
        "",
        "| target | TAP | taplint | maxrec default |",
        "| --- | --- | --- | --- |",
    ]
    for name in targets:
        gate = gates["targets"].get(name, {})
        vosi_facts = gate.get("vosi", {})
        lint = gate.get("taplint", {})
        lines.append(
            f"| {name} | {','.join(vosi_facts.get('tap_versions', []) or ['?'])} |"
            f" {'PASS' if lint.get('passed') else 'FAIL'}"
            f" ({lint.get('errors_total', '?')} errors) |"
            f" {vosi_facts.get('maxrec_default', '?')} |"
        )
    agreement = gates.get("agreement", {})
    if agreement:
        lines += [
            "",
            f"Agreement gate: **{len(agreement.get('agreed', []))} classes agree**"
            + (
                f"; disagreeing (excluded): {', '.join(agreement['disagreed'])}"
                if agreement.get("disagreed")
                else ", none disagree"
            )
            + ".",
        ]
    return lines


def _tables(rows: list[dict], targets: list[str], heading: str) -> list[str]:
    cells = aggregate(rows)
    lines: list[str] = []
    formats = sorted({r["response_format"] for r in rows})
    classes = sorted({r["query_class"] for r in rows})
    concurrencies = sorted({r["concurrency"] for r in rows})
    for response_format in formats:
        lines += ["", f"{heading} {response_format}", ""]
        header = "| class | c |"
        rule = "| --- | --- |"
        for name in targets:
            header += f" {name} rps | {name} p95 (s) |"
            rule += " --- | --- |"
        header += " verdict |"
        rule += " --- |"
        lines += [header, rule]
        for cls in classes:
            for concurrency in concurrencies:
                keys = [(cls, response_format, concurrency, t) for t in targets]
                if not all(k in cells for k in keys):
                    continue
                row = f"| {cls} | {concurrency} |"
                for key in keys:
                    rps_txt = _fmt(cells[key]["rps"])
                    if cells[key]["errors"] > ERROR_CEILING:
                        rps_txt += f" ({cells[key]['errors']:.0%} err)"
                    row += f" {rps_txt} | {_fmt(cells[key]['p95'], 3)} |"
                outcome = verdict(cells, keys[0], keys[1]) if len(keys) == 2 else "—"
                row += f" {outcome} |"
                lines.append(row)
    return lines


def render(run_dir: pathlib.Path, out_dir: pathlib.Path) -> pathlib.Path:
    """The docs/performance page for one comparison run directory.

    A run whose rows carry a ``tier`` (a resource-scaling run) renders one
    section per tier, each with its own gate record (``t<tier>-gates.json``)
    and tables; a flat run renders exactly as before.
    """
    rows = json.loads((run_dir / "summary.json").read_text())
    environment = json.loads((run_dir / "environment.json").read_text())
    targets = sorted({r["target"] for r in rows})
    tiers = sorted({r.get("tier") for r in rows}, key=_tier_key)
    scaling = tiers != [None]

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "summary.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS + (["tier"] if scaling else []))
        writer.writeheader()
        for row in rows:
            writer.writerow(_flat(row) | ({"tier": row.get("tier")} if scaling else {}))
    shutil.copy(run_dir / "environment.json", out_dir / "environment.json")
    for extra in ("taplint", "capabilities", "pins"):
        if (run_dir / extra).is_dir():
            shutil.copytree(run_dir / extra, out_dir / extra, dirs_exist_ok=True)

    usage = resources(run_dir, rows)
    if usage:
        with (out_dir / "resources.csv").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=RESOURCE_COLUMNS)
            writer.writeheader()
            for row in rows:
                res = usage.get(_rung_key(row))
                if res:
                    writer.writerow(
                        {
                            k: row.get(k)
                            for k in ("target", "tier", "query_class", "response_format")
                        }
                        | {k: row[k] for k in ("concurrency", "repetition", "requests")}
                        | {"rps": round(row["rps"], 3)}
                        | {k: (round(v, 6) if isinstance(v, float) else v) for k, v in res.items()}
                    )

    lines = [f"# {run_dir.name}", ""] + (SCALING_INTRO if scaling else PARITY_INTRO)
    for tier in tiers:
        prefix = f"t{tier}-" if tier else ""
        gates = json.loads((run_dir / f"{prefix}gates.json").read_text())
        (out_dir / f"{prefix}gates.json").write_text(json.dumps(gates, indent=2, sort_keys=True))
        heading = "##"
        if tier:
            lines += ["", f"## Tier {tier}"]
            heading = "###"
        tier_rows = [r for r in rows if r.get("tier") == tier]
        lines += _gates_section(gates, targets, heading)
        lines += _tables(tier_rows, targets, heading)
        lines += _resource_tables(tier_rows, usage, targets, heading)

    lines += [
        "",
        "## Claims",
        "",
        "This run may claim the relative behaviour *of these versions, on this",
        "hardware, on this corpus, as deployed by their own documentation,",
        "under the recorded resource pins* — nothing else. A `tie` verdict is",
        f"pre-registered: overlapping 95% intervals, or under {RPS_FLOOR:.0%} apart",
        "in throughput. Classes a gate excluded are absent, not hidden.",
        "",
        "## Threats to validity",
        "",
        "- The corpus is generated by egernia's own seeder; its distributions",
        "  may flatter egernia's index choices.",
        "- The query classes descend from egernia's own performance history.",
        "- The team operates egernia expertly and DaCHS from its documentation.",
        "- Single hardware, single run window; versions frozen at the recorded",
        "  digests.",
    ]
    if scaling:
        lines += [
            "- Within a tier the servers were measured one after the other, not",
            "  interleaved: host drift over a tier's hours lands on one server.",
            "  The order alternates between tiers (recorded in `pins/`).",
        ]
    lines += [
        "",
        "## Reproduce with",
        "",
        "```bash",
        "scripts/export_obscore_snapshot.sh benchmarks/tap-compare/corpus",
    ]
    if scaling:
        lines += ["benchmarks/tap-compare/scaling/run.sh"]
    else:
        lines += [
            "docker compose -f docker-compose.yml \\",
            "    -f benchmarks/tap-compare/docker-compose.egernia-pins.yml up -d",
            "docker compose -f benchmarks/tap-compare/docker-compose.dachs.yml up -d",
            "uv run --group tap-compare python benchmarks/tap-compare compare \\",
            f"    --targets {' '.join(targets)} --scenario <scenario>",
        ]
    lines += [
        "```",
        "",
        f"Environment: see `environment.json` (git {environment['git']['sha'][:8]},"
        f" seed {environment['seed']}, corpus {environment['corpus_sha256'][:12]}…).",
    ]
    (out_dir / "index.md").write_text("\n".join(lines) + "\n")
    return out_dir / "index.md"
