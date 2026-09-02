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


def render(run_dir: pathlib.Path, out_dir: pathlib.Path) -> pathlib.Path:
    """The docs/performance page for one comparison run directory."""
    rows = json.loads((run_dir / "summary.json").read_text())
    environment = json.loads((run_dir / "environment.json").read_text())
    gates = json.loads((run_dir / "gates.json").read_text())
    targets = sorted({r["target"] for r in rows})
    cells = aggregate(rows)

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "summary.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(_flat(row))
    shutil.copy(run_dir / "environment.json", out_dir / "environment.json")
    (out_dir / "gates.json").write_text(json.dumps(gates, indent=2, sort_keys=True))
    for extra in ("taplint", "capabilities"):
        if (run_dir / extra).is_dir():
            shutil.copytree(run_dir / extra, out_dir / extra, dirs_exist_ok=True)

    lines = [f"# {run_dir.name}", ""]
    lines += [
        "Same-hardware TAP-server comparison: identical logical corpus, each",
        "server deployed per its own documentation, one target stack running",
        "at a time, the identical seeded query stream, MAXREC pinned on every",
        "request. Every target stack is pinned to the same 8 CPU / 8 GiB",
        "budget: DaCHS in `benchmarks/tap-compare/docker-compose.dachs.yml`",
        "(`cpus: 8`, `mem_limit: 8g`), egernia in",
        "`benchmarks/tap-compare/docker-compose.egernia-pins.yml` (shared",
        "`cpuset` of 8 cores; 8 GiB split 4 db / 2 api / 2 executor).",
        "See `benchmarks/tap-compare/README.md` for the protocol.",
        "",
        "## Gates",
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

    formats = sorted({r["response_format"] for r in rows})
    classes = sorted({r["query_class"] for r in rows})
    concurrencies = sorted({r["concurrency"] for r in rows})
    for response_format in formats:
        lines += ["", f"## {response_format}", ""]
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
        "",
        "## Reproduce with",
        "",
        "```bash",
        "scripts/export_obscore_snapshot.sh benchmarks/tap-compare/corpus",
        "docker compose -f docker-compose.yml \\",
        "    -f benchmarks/tap-compare/docker-compose.egernia-pins.yml up -d",
        "docker compose -f benchmarks/tap-compare/docker-compose.dachs.yml up -d",
        "uv run --group tap-compare python benchmarks/tap-compare compare \\",
        f"    --targets {' '.join(targets)} --scenario <scenario>",
        "```",
        "",
        f"Environment: see `environment.json` (git {environment['git']['sha'][:8]},"
        f" seed {environment['seed']}, corpus {environment['corpus_sha256'][:12]}…).",
    ]
    (out_dir / "index.md").write_text("\n".join(lines) + "\n")
    return out_dir / "index.md"
