"""The HTML report, and the CSV beside it.

The report is written to be read by someone deciding what to optimise next, so
it leads with the bottleneck classification and the invalid-run banner rather
than with a table of every number. Everything else is there underneath, in the
order a question gets asked: what was measured, on what, how it behaved, and
which evidence says why.
"""

from __future__ import annotations

import csv
import html
import json
import math
import pathlib

TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{
    --ink: #16202a; --muted: #5b6b7a; --line: #d8e0e8; --bg: #fbfcfd;
    --accent: #1f4e79; --warn: #b71c1c; --ok: #2e7d32;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0 auto; padding: 2.5rem 1.5rem 6rem; max-width: 68rem;
         font: 16px/1.6 -apple-system, "Segoe UI", Roboto, sans-serif;
         color: var(--ink); background: var(--bg); }}
  h1 {{ font-size: 1.9rem; margin: 0 0 .25rem; letter-spacing: -.02em; }}
  h2 {{ font-size: 1.25rem; margin: 3rem 0 .75rem; padding-bottom: .35rem;
        border-bottom: 2px solid var(--line); }}
  h3 {{ font-size: 1rem; margin: 2rem 0 .5rem; color: var(--accent); }}
  .sub {{ color: var(--muted); margin: 0 0 2rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .875rem;
           margin: .75rem 0 1.5rem; }}
  th, td {{ text-align: left; padding: .45rem .6rem;
            border-bottom: 1px solid var(--line); }}
  th {{ background: #eef2f6; font-weight: 600; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .wrap {{ overflow-x: auto; }}
  figure {{ margin: 1.5rem 0; }}
  figure img {{ width: 100%; border: 1px solid var(--line); border-radius: 6px;
                background: white; }}
  figcaption {{ color: var(--muted); font-size: .85rem; margin-top: .5rem; }}
  .banner {{ padding: 1rem 1.25rem; border-radius: 8px; margin: 1.5rem 0;
             border-left: 5px solid; }}
  .banner.bad {{ background: #fdecea; border-color: var(--warn); }}
  .banner.good {{ background: #edf7ee; border-color: var(--ok); }}
  .banner.info {{ background: #eef2f6; border-color: var(--accent); }}
  code, pre {{ font: 13px/1.5 ui-monospace, "SF Mono", Menlo, monospace; }}
  pre {{ background: #f2f5f8; padding: .9rem; border-radius: 6px;
         overflow-x: auto; }}
  .pill {{ display: inline-block; padding: .1rem .5rem; border-radius: 999px;
           background: #eef2f6; font-size: .8rem; margin-right: .35rem; }}
  .missing {{ color: var(--muted); font-style: italic; }}
  details {{ margin: .5rem 0; }}
  summary {{ cursor: pointer; font-weight: 600; }}
</style>
<h1>{title}</h1>
<p class="sub">{subtitle}</p>
{body}
"""


def _table(headers: list[str], rows: list[list], numeric: set[int] | None = None) -> str:
    numeric = numeric or set()
    head = "".join(
        f'<th class="num">{html.escape(str(h))}</th>'
        if i in numeric
        else f"<th>{html.escape(str(h))}</th>"
        for i, h in enumerate(headers)
    )
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="num">{html.escape(_fmt(v))}</td>'
            if i in numeric
            else f"<td>{html.escape(_fmt(v))}</td>"
            for i, v in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    return (
        f'<div class="wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if math.isnan(value):
            return "—"
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        if abs(value) >= 1:
            return f"{value:.3g}"
        return f"{value:.4g}"
    return str(value)


def _figure(figure) -> str:
    if figure.path is None:
        return (
            f"<figure><h3>{html.escape(figure.title)}</h3>"
            f'<p class="missing">Not plotted — {html.escape(figure.missing_reason)}'
            f"</p></figure>"
        )
    return (
        f"<figure><h3>{html.escape(figure.title)}</h3>"
        f'<img src="plots/{figure.path.name}" alt="{html.escape(figure.title)}">'
        f"<figcaption>{html.escape(figure.caption)}</figcaption></figure>"
    )


def write_csv(summary: dict, path: pathlib.Path) -> None:
    """One row per measurement: the machine-readable form of the report."""
    fields = [
        "key",
        "kind",
        "dataset",
        "database_gib",
        "mode",
        "response_format",
        "concurrency",
        "replicas",
        "workers",
        "offered_rps",
        "repetition",
        "requests",
        "successful",
        "rps",
        "successful_rps",
        "error_fraction",
        "timeout_fraction",
        "latency_mean_s",
        "latency_p50_s",
        "latency_p95_s",
        "latency_p99_s",
        "latency_p999_s",
        "latency_max_s",
        "latency_cv",
        "ttfb_p95_s",
        "response_throughput_bytes_per_s",
        "tap_api_cpu_cores_mean",
        "postgres_cpu_cores_mean",
        "postgres_read_bytes_per_s",
        "cache_hit_ratio",
        "bottleneck",
        "invalid",
    ]
    sizes = {
        name: (info.get("database_bytes") or 0) / 2**30
        for name, info in (summary.get("datasets") or {}).items()
    }
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for run in summary.get("runs", []):
            http = run.get("http") or {}
            latency = http.get("latency") or {}
            resources = run.get("resources") or {}
            postgres = run.get("postgres") or {}
            writer.writerow(
                {
                    "key": run.get("key"),
                    "kind": run.get("kind"),
                    "dataset": run.get("dataset"),
                    "database_gib": round(sizes.get(run.get("dataset"), 0.0), 3),
                    "mode": run.get("mode"),
                    # Defaulted rather than omitted: every measurement had a
                    # response format, including the ones taken before the
                    # suite recorded it, and all of those were CSV.
                    "response_format": run.get("response_format", "csv"),
                    "concurrency": run.get("concurrency"),
                    "replicas": run.get("replicas"),
                    # Empty rather than defaulted for older measurements: the
                    # worker count was not recorded before the worker sweep
                    # existed, and a written 1 would claim it was observed.
                    "workers": run.get("workers"),
                    "offered_rps": run.get("offered_rps"),
                    "repetition": run.get("repetition"),
                    "requests": http.get("requests"),
                    "successful": http.get("successful"),
                    "rps": http.get("rps"),
                    "successful_rps": http.get("successful_rps"),
                    "error_fraction": http.get("error_fraction"),
                    "timeout_fraction": http.get("timeout_fraction"),
                    "latency_mean_s": latency.get("mean_s"),
                    "latency_p50_s": latency.get("p50_s"),
                    "latency_p95_s": latency.get("p95_s"),
                    "latency_p99_s": latency.get("p99_s"),
                    "latency_p999_s": latency.get("p99_9_s"),
                    "latency_max_s": latency.get("max_s"),
                    "latency_cv": latency.get("coefficient_of_variation"),
                    "ttfb_p95_s": (http.get("ttfb") or {}).get("p95_s"),
                    "response_throughput_bytes_per_s": http.get("response_throughput_bytes_per_s"),
                    "tap_api_cpu_cores_mean": resources.get("tap_api_cpu_cores_mean"),
                    "postgres_cpu_cores_mean": resources.get("postgres_cpu_cores_mean"),
                    "postgres_read_bytes_per_s": resources.get("postgres_fs_read_bytes_mean"),
                    "cache_hit_ratio": postgres.get("cache_hit_ratio"),
                    "bottleneck": (run.get("bottleneck") or [{}])[0].get("classification"),
                    "invalid": run.get("invalid", False),
                }
            )


def render(run_dir: pathlib.Path, summary: dict, figures: list) -> pathlib.Path:
    from . import stats as stats_mod

    environment = summary.get("environment") or {}
    git = environment.get("git") or {}
    sections: list[str] = []

    # -- validity, first, because it conditions everything below ------------
    invalid = summary.get("invalid") or []
    if invalid:
        items = "".join(
            f"<li><strong>{html.escape(str(entry.get('reason')))}</strong> — "
            f"{html.escape(str(entry.get('detail', '')))}</li>"
            for entry in invalid
        )
        sections.append(
            f'<div class="banner bad"><strong>This run is marked invalid.</strong>'
            f"<ul>{items}</ul>The numbers are kept because they are the evidence "
            "of what went wrong, but they do not describe the service under the "
            "conditions intended.</div>"
        )
    else:
        sections.append(
            '<div class="banner good">All validity guards passed: no swapping, no '
            "OOM kills, no unexpected restarts, disk headroom kept, load "
            "generator below its own ceiling, and monitoring coverage complete."
            "</div>"
        )

    # -- headline -----------------------------------------------------------
    headline = summary.get("headline") or {}
    if headline:
        sections.append("<h2>What this run says</h2>")
        sections.append(
            _table(
                ["finding", "value", "evidence"],
                [[k, v.get("value"), v.get("evidence")] for k, v in headline.items()],
                numeric={1},
            )
        )

    bottlenecks = summary.get("bottleneck_tally") or {}
    if bottlenecks:
        sections.append("<h3>Bottleneck classification</h3>")
        sections.append(
            _table(
                ["classification", "measurements", "what it means"],
                [
                    [name, entry["count"], entry["explanation"]]
                    for name, entry in sorted(bottlenecks.items(), key=lambda kv: -kv[1]["count"])
                ],
                numeric={1},
            )
        )

    # -- where the request's CPU goes (package 18) --------------------------
    profile = summary.get("profile") or {}
    if profile:
        sections.append("<h2>Per-request CPU</h2>")
        rungs = profile.get("rungs") or {}
        sections.append(
            _table(
                [
                    "rung",
                    "authenticated",
                    "rps",
                    "p95 (ms)",
                    "API CPU (cores)",
                    "CPU ms/request",
                    "errors",
                ],
                [
                    [
                        name,
                        "yes" if rung["authenticated"] else "no",
                        rung["rps"]["mean"],
                        rung["p95_ms"],
                        rung["api_cpu_cores"]["mean"],
                        rung["cpu_ms_per_request"]["mean"],
                        f"{100 * rung['error_fraction']:.2f}%",
                    ]
                    for name, rung in rungs.items()
                ],
                numeric={2, 3, 4, 5},
            )
        )
        attribution = profile.get("attribution") or {}
        if attribution:
            sections.append(
                f"<p>{attribution['samples']:,} GIL-held samples of the saturated worker; "
                f"{100 * attribution['named_fraction']:.1f}% attributed to a named subsystem, "
                f"{100 * attribution['application_fraction']:.1f}% of it in the application "
                "rather than in the server, the router or the event loop.</p>"
            )
            sections.append(
                _table(
                    ["subsystem", "ms/request", "share"],
                    [
                        [name, ms, f"{100 * share:.1f}%"]
                        for name, ms, share in stats_mod.subsystem_shares(attribution)
                    ],
                    numeric={1},
                )
            )
            sections.append("<h3>Busiest named frames</h3>")
            sections.append(
                _table(
                    ["frame", "share of GIL-held samples"],
                    [
                        [entry["frame"], f"{100 * entry['fraction']:.1f}%"]
                        for entry in attribution.get("top_frames") or []
                    ],
                )
            )
        cost = profile.get("authentication_cost") or {}
        if cost:
            sections.append("<h3>What a verified bearer token costs</h3>")
            sections.append(
                _table(
                    ["rung", "rps", "throughput cost", "CPU ms/request", "added ms/request"],
                    [
                        [name, rps, f"{100 * throughput_cost:.1f}%", cpu_ms, added_ms]
                        for name, rps, throughput_cost, cpu_ms, added_ms in (
                            stats_mod.auth_cost_rows(cost)
                        )
                    ],
                    numeric={1, 3, 4},
                )
            )

    # -- environment --------------------------------------------------------
    sections.append("<h2>Environment</h2>")
    cluster = environment.get("cluster") or {}
    host = environment.get("host") or {}
    sections.append(
        _table(
            ["property", "value"],
            [
                ["commit", f"{git.get('sha', '')[:12]}{' (dirty)' if git.get('dirty') else ''}"],
                ["branch", git.get("branch")],
                ["host", f"{host.get('platform')} / {host.get('machine')}"],
                ["host CPUs", host.get("cpu_count")],
                ["Kubernetes", cluster.get("kubernetes")],
                [
                    "node capacity",
                    f"{cluster.get('node_cpu_capacity')} CPU, "
                    f"{cluster.get('node_memory_capacity')}",
                ],
                ["container runtime", cluster.get("container_runtime")],
                ["KEDA", cluster.get("keda_image")],
                ["PostgreSQL", cluster.get("postgres")],
                ["extensions", cluster.get("postgres_extensions")],
                [
                    "images",
                    "<br>".join(
                        f"{k} {v[:19]}" for k, v in (environment.get("images") or {}).items()
                    ),
                ],
                ["seed", environment.get("seed")],
                ["corpus sha256", (environment.get("corpus_sha256") or "")[:16]],
                ["chart values sha256", (environment.get("chart_values_sha256") or "")[:16]],
            ],
        )
    )

    # -- datasets -----------------------------------------------------------
    datasets = summary.get("datasets") or {}
    if datasets:
        sections.append("<h2>Datasets</h2>")
        sections.append(
            "<p>Sizes are <code>pg_database_size()</code> with indexes, not row "
            "counts multiplied by an assumed width.</p>"
        )
        rows = []
        for name, info in sorted(datasets.items()):
            rows.append(
                [
                    name,
                    (info.get("database_bytes") or 0) / 2**30,
                    info.get("obscore_rows"),
                    sum((info.get("row_counts") or {}).values()),
                    info.get("index_to_table_ratio"),
                    info.get("observations_generated"),
                    info.get("seconds"),
                ]
            )
        sections.append(
            _table(
                [
                    "dataset",
                    "size (GiB)",
                    "ObsCore rows",
                    "total rows",
                    "index/table",
                    "observations",
                    "generation (s)",
                ],
                rows,
                numeric={1, 2, 3, 4, 5, 6},
            )
        )
        per_table = []
        for name, info in sorted(datasets.items()):
            for table, count in sorted((info.get("row_counts") or {}).items()):
                per_table.append(
                    [
                        name,
                        table,
                        count,
                        (info.get("table_bytes") or {}).get(table, 0) / 2**20,
                        (info.get("index_bytes") or {}).get(table, 0) / 2**20,
                    ]
                )
        if per_table:
            sections.append(
                "<details><summary>Per-table row counts and sizes</summary>"
                + _table(
                    ["dataset", "table", "rows", "table (MiB)", "indexes (MiB)"],
                    per_table,
                    numeric={2, 3, 4},
                )
                + "</details>"
            )

    # -- measurements -------------------------------------------------------
    runs = summary.get("runs") or []
    if runs:
        sections.append("<h2>Measurements</h2>")
        rows = []
        for run in runs:
            http = run.get("http") or {}
            latency = http.get("latency") or {}
            rows.append(
                [
                    run.get("kind"),
                    run.get("dataset"),
                    run.get("mode"),
                    run.get("response_format", "csv"),
                    run.get("concurrency"),
                    run.get("replicas"),
                    run.get("offered_rps"),
                    run.get("repetition"),
                    http.get("requests"),
                    http.get("rps"),
                    100 * (http.get("error_fraction") or 0.0),
                    1000 * (latency.get("p50_s") or 0.0),
                    1000 * (latency.get("p95_s") or 0.0),
                    1000 * (latency.get("p99_s") or 0.0),
                    (run.get("bottleneck") or [{}])[0].get("classification"),
                ]
            )
        sections.append(
            _table(
                [
                    "kind",
                    "dataset",
                    "mode",
                    "format",
                    "clients",
                    "replicas",
                    "offered rps",
                    "rep",
                    "requests",
                    "rps",
                    "errors %",
                    "p50 ms",
                    "p95 ms",
                    "p99 ms",
                    "bottleneck",
                ],
                rows,
                numeric={4, 5, 6, 7, 8, 9, 10, 11, 12, 13},
            )
        )

    # -- workers against replicas --------------------------------------------
    grid = summary.get("worker_capacity") or []
    if grid:
        sections.append("<h2>Workers against replicas</h2>")
        sections.append(
            "<p>The same closed-loop ladder at every (workers, replicas) "
            "point — same corpus, same seeds — so two rows differ in the "
            "fleet's shape and nothing else. A worker costs no pod but holds "
            "its own connection pool, so each row states the arithmetic its "
            "shape implies at the database.</p>"
        )
        sections.append(
            _table(
                [
                    "workers",
                    "replicas",
                    "processes",
                    "capacity (rps)",
                    "rps per process",
                    "ceiling?",
                    "pool ceiling (connections)",
                    "evidence",
                ],
                [
                    [
                        row["workers"],
                        row["replicas"],
                        row["worker_processes"],
                        row["rps"],
                        row["rps"] / row["worker_processes"],
                        "ceiling" if row["bracketed"] else "open-ended",
                        f"{row['connection_ceiling']}"
                        + (
                            " — exceeds max_connections"
                            if row.get("exceeds_max_connections")
                            else ""
                        ),
                        row["key"],
                    ]
                    for row in sorted(grid, key=lambda r: (r["workers"], r["replicas"]))
                ],
                numeric={0, 1, 2, 3, 4},
            )
        )

    # -- result formats -----------------------------------------------------
    comparison = summary.get("format_comparison") or []
    if comparison:
        sections.append("<h2>Result formats</h2>")
        sections.append(
            "<p>The same rows out through every writer, at fixed concurrency, so "
            "the difference between two lines is the writer and the bytes it "
            "produced. Cheapest first, per query class.</p>"
        )
        cheapest = {
            query_class: min(
                r["latency_p95_s"] for r in comparison if r["query_class"] == query_class
            )
            for query_class in {r["query_class"] for r in comparison}
        }
        rows = []
        for row in sorted(comparison, key=lambda r: (r["query_class"], r["latency_p95_s"])):
            floor = cheapest[row["query_class"]] or None
            rows.append(
                [
                    row["query_class"],
                    row["response_format"],
                    row["repetitions"],
                    row["requests"],
                    row["rps"],
                    1000 * row["latency_p50_s"],
                    1000 * row["latency_p95_s"],
                    row["mean_response_bytes"] / 2**20,
                    (row["latency_p95_s"] / floor) if floor else None,
                ]
            )
        sections.append(
            _table(
                [
                    "class",
                    "format",
                    "reps",
                    "requests",
                    "rps",
                    "p50 ms",
                    "p95 ms",
                    "MiB / response",
                    "vs cheapest",
                ],
                rows,
                numeric={2, 3, 4, 5, 6, 7, 8},
            )
        )

    # -- autoscaling --------------------------------------------------------
    keda = summary.get("keda") or []
    if keda:
        sections.append("<h2>Autoscaling</h2>")
        rows = []
        for scenario in keda:
            latencies = (scenario.get("timings") or {}).get("latencies_s") or {}
            behaviour = scenario.get("behaviour") or {}
            rows.append(
                [
                    scenario.get("id"),
                    scenario.get("description"),
                    latencies.get("detection"),
                    latencies.get("hpa_decision"),
                    latencies.get("pod_provisioning"),
                    latencies.get("routing"),
                    latencies.get("total_scale_out"),
                    latencies.get("capacity_recovery"),
                    behaviour.get("peak_replicas"),
                    behaviour.get("scale_events"),
                    behaviour.get("direction_reversals"),
                    behaviour.get("replica_seconds"),
                ]
            )
        sections.append(
            _table(
                [
                    "scenario",
                    "description",
                    "detect s",
                    "HPA s",
                    "provision s",
                    "routing s",
                    "total s",
                    "recovery s",
                    "peak",
                    "events",
                    "reversals",
                    "replica-s",
                ],
                rows,
                numeric=set(range(2, 12)),
            )
        )
        sections.append(
            "<p>Stages: T0 load changed, T1 scaler metric crossed its threshold, "
            "T2 HPA changed its request, T3-T6 pod created / scheduled / started / "
            "Ready, T7 served traffic, T8 p95 back inside the SLO. A dash means "
            "the stage could not be established from the evidence — recorded "
            "rather than estimated.</p>"
        )
        for scenario in keda:
            notes = (scenario.get("timings") or {}).get("notes") or []
            if notes:
                sections.append(
                    f'<p class="missing">{html.escape(scenario["id"])}: '
                    + html.escape("; ".join(notes))
                    + "</p>"
                )

    # -- postgres -----------------------------------------------------------
    plans = summary.get("plan_flags") or {}
    if plans:
        sections.append("<h2>Query plans</h2>")
        sections.append(
            _table(
                ["flag", "plans affected", "why it matters"],
                [
                    [name, entry["count"], entry["explanation"]]
                    for name, entry in sorted(plans.items(), key=lambda kv: -kv[1]["count"])
                ],
                numeric={1},
            )
        )

    # -- figures ------------------------------------------------------------
    sections.append("<h2>Plots</h2>")
    for figure in figures:
        sections.append(_figure(figure))

    # -- artefacts ----------------------------------------------------------
    sections.append("<h2>Artefacts</h2>")
    listing = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_file() and path.suffix in (
            ".parquet",
            ".csv",
            ".json",
            ".jsonl",
            ".yaml",
            ".txt",
        ):
            listing.append(
                [
                    str(path.relative_to(run_dir)),
                    path.stat().st_size / 1024,
                ]
            )
    sections.append(_table(["file", "size (KiB)"], listing, numeric={1}))

    title = f"TAP benchmark — {summary.get('scenario', 'run')}"
    subtitle = (
        f"{run_dir.name} · commit {git.get('sha', '')[:8]} · "
        f"{len(runs)} measurements · seed {environment.get('seed')}"
    )
    output = run_dir / "report.html"
    output.write_text(
        TEMPLATE.format(
            title=html.escape(title), subtitle=html.escape(subtitle), body="\n".join(sections)
        )
    )
    return output


def load_summary(run_dir: pathlib.Path) -> dict:
    return json.loads((run_dir / "summary.json").read_text())
