"""Publish a run's results into the documentation site.

The HTML report inside a results directory is for whoever ran the benchmark.
This is for everyone else: a page in the MkDocs site, which CI already deploys
to GitHub Pages, carrying the graphs and the numbers behind them.

What gets published is deliberately narrow. PNGs, the summary CSV and the
markdown — a few megabytes. Not the Parquet samples (hundreds of megabytes,
and useless without the tooling to read them) and not the SVGs, because a
scatter of a hundred thousand requests is a megabyte of vector paths that no
browser enjoys. The full artefacts stay in the results directory, and the page
says where they were produced so they can be found.

Published pages accumulate rather than replace: `index.md` always describes the
newest run and links the rest. A performance page that silently replaces last
month's numbers with this month's is how a regression goes unnoticed.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
import shutil

log = logging.getLogger("egernia_bench.publish")

REPO = pathlib.Path(__file__).resolve().parents[4]
DOCS = REPO / "docs" / "performance"

# The plots worth putting in front of a reader, in the order the story is told:
# what the service does, how it scales out, what the data size does to it, and
# where the time goes.
FEATURED = (
    ("rps_vs_concurrency", "Throughput against offered concurrency"),
    ("latency_vs_concurrency", "Latency percentiles against concurrency"),
    ("errors_vs_concurrency", "Errors against concurrency"),
    ("rps_vs_replicas", "Throughput against replica count"),
    ("scaling_efficiency", "Scaling efficiency"),
    ("rps_vs_workers", "Throughput against workers and replicas"),
    ("rps_vs_size", "Throughput against database size"),
    ("latency_vs_size", "Latency against database size"),
    ("cache_hit_vs_size", "Buffer cache hit ratio against database size"),
    ("tap_cpu_vs_throughput", "API CPU against throughput"),
    ("postgres_cpu_vs_throughput", "PostgreSQL CPU against throughput"),
    ("postgres_io_vs_throughput", "PostgreSQL read I/O against throughput"),
    ("query_class_rps", "Throughput by query class"),
    ("query_class_latency", "Latency by query class"),
    ("class_size_heatmap", "Query class against database size"),
    ("result_size_vs_latency", "Latency against response size"),
    ("run_to_run_variability", "Run-to-run variability"),
)


def _fmt(value, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.{digits}f}{suffix}"
    return f"{value}{suffix}"


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_No measurements in this family._\n"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(out) + "\n"


def _concurrency_table(summary: dict) -> str:
    from . import stats as stats_mod

    buckets: dict[tuple[str, int], list[dict]] = {}
    for run in summary.get("runs", []):
        if run.get("kind") not in ("concurrency", "saturation"):
            continue
        buckets.setdefault((run["dataset"], run["concurrency"]), []).append(run)
    rows = []
    for (dataset, concurrency), group in sorted(buckets.items()):
        rps = stats_mod.mean_ci([r["http"]["rps"] for r in group])
        p95 = stats_mod.mean_ci([r["http"]["latency"]["p95_s"] * 1000 for r in group])
        errors = max(r["http"]["error_fraction"] for r in group)
        bottleneck = group[0]["bottleneck"][0]["classification"]
        interval = (
            f" ± {rps['ci95_high'] - rps['mean']:.1f}" if rps.get("ci95_high") is not None else ""
        )
        rows.append(
            [
                dataset,
                concurrency,
                len(group),
                f"{_fmt(rps['mean'])}{interval}",
                _fmt(p95["mean"], 0),
                f"{100 * errors:.2f}%",
                f"`{bottleneck}`",
            ]
        )
    return _table(
        ["dataset", "clients", "reps", "requests/s", "p95 (ms)", "errors", "bottleneck"],
        rows,
    )


def _replica_table(summary: dict) -> str:
    """One row per replica count, from the capacities the analysis settled on.

    The efficiency column is the headline's claim repeated per row, so it is
    filled in only where the headline would state one: both that replica count
    and the single-replica baseline pushed past what they could serve. Where
    the ladder never bracketed a ceiling the column reads "—" and the "ceiling"
    column says why, rather than dividing two rates nobody's limit produced.
    """
    capacities = summary.get("replica_capacity") or []
    by_key = {
        r["key"]: r
        for r in summary.get("runs", [])
        if r.get("kind") in ("fixed_replicas", "replica_sweep")
    }
    if not capacities or not by_key:
        return ""
    baseline = next((c for c in capacities if c["replicas"] == 1), None)
    rows = []
    for capacity in sorted(capacities, key=lambda c: c["replicas"]):
        run = by_key.get(capacity["key"])
        if run is None:
            continue
        replicas = capacity["replicas"]
        comparable = (
            baseline and baseline["bracketed"] and capacity["bracketed"] and baseline["rps"]
        )
        efficiency = capacity["rps"] / (replicas * baseline["rps"]) if comparable else None
        rows.append(
            [
                replicas,
                _fmt(capacity["rps"]),
                _fmt(capacity["offered_rps"]),
                _fmt(run["http"]["latency"]["p95_s"] * 1000, 0),
                "reached" if capacity["bracketed"] else "not reached",
                _fmt(efficiency, 3) if efficiency is not None else "—",
                f"`{run['bottleneck'][0]['classification']}`",
            ]
        )
    return _table(
        [
            "replicas",
            "successful rps",
            "offered rps",
            "p95 (ms)",
            "ceiling",
            "efficiency",
            "bottleneck",
        ],
        rows,
    )


def _worker_table(summary: dict) -> str:
    """One row per (workers, replicas) point, connection arithmetic included.

    The pool ceiling is printed with every capacity because the axes are not
    interchangeable at the database: a worker costs no pod but holds its own
    pool, and the grid deliberately includes shapes the configured
    max_connections cannot honour.
    """
    capacities = summary.get("worker_capacity") or []
    by_key = {r["key"]: r for r in summary.get("runs", []) if r.get("kind") == "worker_sweep"}
    if not capacities or not by_key:
        return ""
    rows = []
    for capacity in sorted(capacities, key=lambda c: (c["workers"], c["replicas"])):
        run = by_key.get(capacity["key"])
        if run is None:
            continue
        ceiling = str(capacity["connection_ceiling"])
        if capacity.get("exceeds_max_connections"):
            ceiling += " ⚠ exceeds max_connections"
        rows.append(
            [
                capacity["workers"],
                capacity["replicas"],
                capacity["worker_processes"],
                _fmt(capacity["rps"]),
                _fmt(capacity["rps"] / capacity["worker_processes"]),
                _fmt(run["http"]["latency"]["p95_s"] * 1000, 0),
                "reached" if capacity["bracketed"] else "not reached",
                ceiling,
                f"`{run['bottleneck'][0]['classification']}`",
            ]
        )
    return _table(
        [
            "workers",
            "replicas",
            "processes",
            "successful rps",
            "rps/process",
            "p95 (ms)",
            "ceiling",
            "pool ceiling (connections)",
            "bottleneck",
        ],
        rows,
    )


def _profile_section(summary: dict) -> list[str]:
    """Package 18: the per-request CPU attribution, for the public page.

    Written as three tables rather than one, because they are three different
    kinds of claim: what each rung measured, what a sampling profiler says the
    CPU is spent on, and what enabling authentication cost. Collapsing them
    would invite a reader to treat the profiler's shares as measured
    throughput, which is exactly the mistake the family is arranged to avoid.
    """
    from . import stats as stats_mod

    profile = summary.get("profile") or {}
    if not profile:
        return []
    parts = [
        "## Per-request CPU",
        "",
        f"One `tap-api` worker at `replicas: 1`, `workers: 1`, "
        f"{profile.get('concurrency')} concurrent clients. `cpu ms/request` is the",
        "pod's own CPU accounting over the window divided by the requests it",
        "served, so it is a measured total and not a profiler's estimate.",
        "",
    ]
    rungs = profile.get("rungs") or {}
    parts.append(
        _table(
            [
                "rung",
                "token",
                "requests/s",
                "p95 (ms)",
                "API CPU (cores)",
                "cpu ms/request",
                "errors",
            ],
            [
                [
                    f"`{name}`",
                    "yes" if rung["authenticated"] else "no",
                    _fmt(rung["rps"]["mean"]),
                    _fmt(rung["p95_ms"], 0),
                    _fmt(rung["api_cpu_cores"]["mean"], 3),
                    _fmt(rung["cpu_ms_per_request"]["mean"], 2),
                    f"{100 * rung['error_fraction']:.2f}%",
                ]
                for name, rung in rungs.items()
            ],
        )
    )

    attribution = profile.get("attribution") or {}
    if attribution:
        parts += [
            "### Where it goes",
            "",
            f"{attribution['samples']:,} GIL-held stacks sampled at 100 Hz while the",
            "worker was saturated. A sample is attributed to the innermost frame that",
            "names a subsystem, so stdlib time rolls up to the part of the request",
            f"that spent it. {100 * attribution['named_fraction']:.1f}% of samples reach a",
            f"named subsystem and {100 * attribution['application_fraction']:.1f}% of the",
            "total is the application's own work rather than the server, the router",
            "or the event loop.",
            "",
            _table(
                ["subsystem", "ms/request", "share"],
                [
                    [name, _fmt(ms, 2), f"{100 * share:.1f}%"]
                    for name, ms, share in stats_mod.subsystem_shares(attribution)
                ],
            ),
        ]

    cost = profile.get("authentication_cost") or {}
    if cost:
        parts += [
            "### What a verified bearer token costs",
            "",
            f"Against {_fmt(cost['unauthenticated_rps'])} rps and",
            f"{_fmt(cost['unauthenticated_cpu_ms'], 2)} ms/request unauthenticated — the mean",
            "of the two unauthenticated rungs either side, because the authenticated",
            "ones are separated from them by a chart upgrade and a pod restart.",
            "",
            _table(
                ["rung", "requests/s", "throughput cost", "cpu ms/request", "added ms/request"],
                [
                    [
                        f"`{name}`",
                        _fmt(rps),
                        f"{100 * throughput_cost:.1f}%",
                        _fmt(cpu_ms, 2),
                        _fmt(added_ms, 2),
                    ]
                    for name, rps, throughput_cost, cpu_ms, added_ms in (
                        stats_mod.auth_cost_rows(cost)
                    )
                ],
            ),
            "`authverify` verifies every token and enforces nothing; `authgated`",
            "additionally takes an authorisation decision on the whole query surface.",
            "Tokens are RS256, 2,048-bit, from an in-cluster issuer whose JWKS the",
            "service caches for its configured five minutes — so this is the cost of",
            "verifying a signature per request, not of reaching an IAM per request.",
            "",
        ]
    return parts


def _keda_table(summary: dict) -> str:
    scenarios = summary.get("keda") or []
    if not scenarios:
        return ""
    rows = []
    for scenario in scenarios:
        latencies = (scenario.get("timings") or {}).get("latencies_s") or {}
        behaviour = scenario.get("behaviour") or {}
        failed = [g["name"] for g in scenario.get("guards") or [] if not g["ok"]]
        rows.append(
            [
                f"**{scenario['id']}**" if not failed else f"**{scenario['id']}** ⚠",
                scenario.get("description", ""),
                _fmt(latencies.get("detection")),
                _fmt(latencies.get("hpa_decision")),
                _fmt(latencies.get("pod_provisioning")),
                _fmt(latencies.get("routing")),
                _fmt(latencies.get("total_scale_out")),
                _fmt(latencies.get("capacity_recovery")),
                behaviour.get("peak_replicas", "—"),
                behaviour.get("scale_events", "—"),
                behaviour.get("direction_reversals", "—"),
            ]
        )
    return _table(
        [
            "scenario",
            "profile",
            "detect (s)",
            "HPA (s)",
            "provision (s)",
            "routing (s)",
            "total (s)",
            "recovery (s)",
            "peak",
            "events",
            "reversals",
        ],
        rows,
    )


def _dataset_table(summary: dict) -> str:
    rows = []
    for name, info in sorted((summary.get("datasets") or {}).items()):
        rows.append(
            [
                name,
                _fmt((info.get("database_bytes") or 0) / 2**30, 2, " GiB"),
                f"{info.get('obscore_rows', 0):,}",
                f"{sum((info.get('row_counts') or {}).values()):,}",
                _fmt(info.get("index_to_table_ratio"), 2),
                _fmt(info.get("seconds"), 0, " s"),
            ]
        )
    return _table(
        ["dataset", "size", "ObsCore rows", "total rows", "index/table", "generation"],
        rows,
    )


# Fields that describe the person or the machine rather than the measurement.
# runs.environment() no longer records them, but runs captured before that
# still carry them, and publish() is the step that would put them on a public
# site — so it is the step that has to drop them.
IDENTIFYING_HOST_FIELDS = ("hostname", "user")


def _deidentified(environment: dict) -> dict:
    host = environment.get("host")
    if not isinstance(host, dict):
        return environment
    return {
        **environment,
        "host": {k: v for k, v in host.items() if k not in IDENTIFYING_HOST_FIELDS},
    }


def publish(run_dir: pathlib.Path, *, docs_dir: pathlib.Path | None = None) -> pathlib.Path:
    """Copy a run's graphs into the docs site and write its page."""
    docs_dir = docs_dir or DOCS
    summary = json.loads((run_dir / "summary.json").read_text())
    run_id = run_dir.name
    target = docs_dir / run_id
    (target / "plots").mkdir(parents=True, exist_ok=True)

    published = []
    for name, title in FEATURED:
        source = run_dir / "plots" / f"{name}.png"
        if source.exists():
            shutil.copy2(source, target / "plots" / f"{name}.png")
            published.append((name, title))
    # The autoscaling dashboards, one per scenario, whatever they are called.
    for source in sorted((run_dir / "plots").glob("keda_*.png")):
        shutil.copy2(source, target / "plots" / source.name)
        published.append((source.stem, f"Autoscaling timeline — {source.stem[5:]}"))
    for extra in ("summary.csv", "dataset.json"):
        if (run_dir / extra).exists():
            shutil.copy2(run_dir / extra, target / extra)
    source = run_dir / "environment.json"
    if source.exists():
        environment = _deidentified(json.loads(source.read_text()))
        (target / "environment.json").write_text(
            json.dumps(environment, indent=2, sort_keys=True) + "\n"
        )

    page = target / "index.md"
    page.write_text(_render(summary, run_id, published))
    log.info("published %s -> %s", run_id, page)
    _write_index(docs_dir)
    return page


def _render(summary: dict, run_id: str, published: list[tuple[str, str]]) -> str:
    environment = summary.get("environment") or {}
    git = environment.get("git") or {}
    cluster = environment.get("cluster") or {}
    host = environment.get("host") or {}
    generated = datetime.datetime.fromtimestamp(
        summary.get("generated_at") or 0, datetime.UTC
    ).strftime("%Y-%m-%d %H:%M UTC")

    parts: list[str] = [
        f"# {summary.get('scenario', 'benchmark').replace('-', ' ').title()} — {run_id[:16]}",
        "",
        f"Run `{run_id}` · commit `{git.get('sha', '')[:12]}`"
        f"{' (working tree dirty)' if git.get('dirty') else ''} · {generated}",
        "",
    ]

    invalid = summary.get("invalid") or []
    if invalid:
        reasons = "; ".join(str(entry.get("reason")) for entry in invalid[:6])
        parts += [
            '!!! danger "This run is marked invalid"',
            f"    {reasons}",
            "",
            "    The numbers below are kept as evidence of what went wrong. They do",
            "    not describe the service under the conditions intended.",
            "",
        ]
    else:
        parts += [
            '!!! success "Validity guards passed"',
            "    No swapping, no OOM kills, no unexpected restarts, disk headroom",
            "    kept, load generator below its own ceiling, monitoring coverage",
            "    complete.",
            "",
        ]

    headline = summary.get("headline") or {}
    if headline:
        parts += ["## Headline", ""]
        parts.append(
            _table(
                ["finding", "value", "evidence"],
                [
                    [k, f"**{_fmt(v.get('value'))}**", v.get("evidence", "")]
                    for k, v in headline.items()
                ],
            )
        )

    parts += ["## What was measured", "", _dataset_table(summary)]
    parts += [
        "Sizes are `pg_database_size()` with indexes — not row counts times an",
        "assumed row width. One database is grown through every target, so each",
        "tier is a genuine prefix of the next.",
        "",
    ]

    parts += ["## Throughput and latency against concurrency", "", _concurrency_table(summary)]
    parts += [
        "Intervals are 95% Student-t across repetitions. The sweep stops when two",
        "saturation signals agree, so the last row of a dataset is where that",
        "dataset's ceiling was found rather than where the ladder ran out.",
        "",
    ]

    replicas = _replica_table(summary)
    if replicas:
        parts += [
            "## Scaling out",
            "",
            replicas,
            "Each row is the highest rate that replica count served inside the",
            "SLO. **ceiling** is whether the ladder went past it: `reached` means",
            "a valid higher rate failed, so the rps beside it is a limit.",
            "Efficiency is `throughput(N) / (N x throughput(1))` and is filled in",
            "only where both that row and the single-replica row reached a",
            "ceiling — otherwise the ratio would describe the rates offered, not",
            "the ones the service could not exceed. Where it is filled in, the",
            "shortfall is what the replicas contend over: one PostgreSQL.",
            "",
        ]

    workers = _worker_table(summary)
    if workers:
        parts += [
            "## Workers against replicas",
            "",
            workers,
            "The same closed-loop ladder at every (workers, replicas) point —",
            "same host, same corpus, same seeds — so two rows differ in the",
            "fleet's shape and nothing else. A worker costs no pod but holds its",
            "own connection pool: the **pool ceiling** column is",
            "`replicas x workers x dbPoolMax`, which is what the shape can open",
            "against the database's `max_connections`.",
            "",
        ]

    keda = _keda_table(summary)
    if keda:
        parts += [
            "## Autoscaling",
            "",
            keda,
            "Stages: **detect** is the scaler's metric crossing its threshold,",
            "**HPA** the replica request changing, **provision** Pod creation to",
            "Ready, **routing** Ready to serving traffic, **recovery** the load",
            "change to p95 back inside the SLO. A dash means the stage could not",
            "be established from the evidence — recorded rather than estimated.",
            "A ⚠ on the scenario means one of its validity guards failed, so its",
            "timings describe conditions other than the ones intended.",
            "",
        ]

    parts += _profile_section(summary)

    bottlenecks = summary.get("bottleneck_tally") or {}
    if bottlenecks:
        parts += [
            "## Where the limit was",
            "",
            _table(
                ["classification", "measurements", "meaning"],
                [
                    [f"`{name}`", entry["count"], entry["explanation"]]
                    for name, entry in sorted(bottlenecks.items(), key=lambda kv: -kv[1]["count"])
                ],
            ),
        ]

    plans = summary.get("plan_flags") or {}
    if plans:
        parts += [
            "## Query plans",
            "",
            _table(
                ["flag", "plans", "why it matters"],
                [
                    [f"`{name}`", entry["count"], entry["explanation"]]
                    for name, entry in sorted(plans.items(), key=lambda kv: -kv[1]["count"])
                ],
            ),
        ]

    parts += ["## Graphs", ""]
    for name, title in published:
        parts += [f"### {title}", "", f"![{title}](plots/{name}.png)", ""]

    parts += [
        "## Environment",
        "",
        _table(
            ["property", "value"],
            [
                ["host", f"{host.get('platform')} ({host.get('cpu_count')} CPUs)"],
                ["Kubernetes", cluster.get("kubernetes")],
                [
                    "node capacity",
                    f"{cluster.get('node_cpu_capacity')} CPU, "
                    f"{cluster.get('node_memory_capacity')}",
                ],
                ["KEDA", cluster.get("keda_image")],
                ["PostgreSQL", cluster.get("postgres")],
                ["extensions", cluster.get("postgres_extensions")],
                ["seed", environment.get("seed")],
                ["corpus sha256", f"`{(environment.get('corpus_sha256') or '')[:16]}`"],
                ["chart values sha256", f"`{(environment.get('chart_values_sha256') or '')[:16]}`"],
            ],
        ),
        "",
        "[Download the per-measurement CSV](summary.csv) ·",
        "[environment.json](environment.json) · [dataset.json](dataset.json)",
        "",
        "Raw per-request samples and Prometheus series stay with the run that",
        f"produced them, under `benchmarks/egernia-performance/results/{run_id}/`:",
        "Parquet for every request and every metric, PostgreSQL statistics",
        "before and after, `EXPLAIN` plans, and the exact ScaledObject and HPA",
        "YAML the measurement ran against.",
        "",
    ]
    return "\n".join(parts)


def _run_family(name: str) -> str:
    """The family a run belongs to, from its directory ``<stamp>-<sha>-<family>``.

    Split at most twice, because family names carry their own hyphens
    (``db-scaling``, ``worker-sweep``, ``result-formats``).
    """
    parts = name.split("-", 2)
    return parts[2] if len(parts) == 3 else "unknown"


def _run_datasets(run_dir: pathlib.Path) -> str:
    """The dataset tiers a run measured, from the run's own dataset.json.

    Read rather than inferred: it is the run's record of what it built, and a
    run published before that file existed says so with a dash instead of
    claiming a tier it may not have measured.
    """
    try:
        measured = json.loads((run_dir / "dataset.json").read_text())
    except OSError, ValueError:
        return "—"
    return " ".join(sorted(measured)) if isinstance(measured, dict) and measured else "—"


def _write_index(docs_dir: pathlib.Path) -> pathlib.Path:
    """The landing page: the newest run, the newest of each family, then all."""
    runs = sorted(
        (p for p in docs_dir.glob("*/index.md")),
        key=lambda p: p.parent.name,
        reverse=True,
    )
    lines = [
        "# Performance",
        "",
        "Benchmark results from `benchmarks/egernia-performance`, which measures the",
        "service, PostgreSQL as the data grows, replica scaling and autoscaling",
        "behaviour separately — because they fail separately.",
        "",
        "Each run below is published in full: the graphs, the per-measurement CSV,",
        "and the provenance needed to know whether two runs are comparable at all",
        "(commit, image ids, seed, corpus hash, chart values hash). Runs",
        "accumulate rather than replace, so a regression has somewhere to show up.",
        "",
    ]
    if not runs:
        lines += ["_No runs published yet._", ""]
    else:
        newest = runs[0]
        summary_path = newest.parent / "summary.csv"
        lines += [
            "## Latest",
            "",
            f"[{newest.parent.name}]({newest.parent.name}/index.md)"
            + (f" · [CSV]({newest.parent.name}/summary.csv)" if summary_path.exists() else ""),
            "",
        ]
        # Only db-scaling and full sweep the dataset tiers; every other family
        # pins one dataset on purpose, because varying a second axis would
        # change two things at once. With a single global "latest", a profile
        # or worker sweep published afterwards therefore reads as though the
        # tiers had been dropped — so each family's newest keeps its own row,
        # and the tiers it measured are stated rather than left to be guessed.
        by_family: dict[str, pathlib.Path] = {}
        for run in runs:  # newest first, so the first of each family wins
            by_family.setdefault(_run_family(run.parent.name), run)
        lines += [
            "## Latest by family",
            "",
            _table(
                ["family", "run", "datasets"],
                [
                    [
                        f"`{family}`",
                        f"[{run.parent.name}]({run.parent.name}/index.md)",
                        _run_datasets(run.parent),
                    ]
                    for family, run in sorted(by_family.items())
                ],
            ),
        ]
        if len(runs) > 1:
            lines += ["## All runs", ""]
            for run in runs:
                lines.append(f"- [{run.parent.name}]({run.parent.name}/index.md)")
            lines.append("")
    lines += [
        "## Reading these numbers",
        "",
        "- A figure without an interval is one measurement. Intervals across",
        "  repetitions are 95% Student-t; percentile intervals within a run are",
        "  percentile bootstrap.",
        "- `LOAD_GENERATOR_BOUND` on any measurement means the client was the",
        "  limit, and nothing else from that measurement describes the service.",
        "- A run marked invalid is published anyway, with the reason. The samples",
        "  are the evidence of what went wrong.",
        "- Two throughputs closer together than the run-to-run variability plot",
        "  shows are the same throughput.",
        "",
    ]
    path = docs_dir / "index.md"
    path.write_text("\n".join(lines))
    return path
