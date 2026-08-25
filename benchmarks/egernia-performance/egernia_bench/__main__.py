"""Command line for the benchmark suite.

    python -m egernia_bench smoke
    python -m egernia_bench db-scaling
    python -m egernia_bench fixed-scaling
    python -m egernia_bench keda
    python -m egernia_bench result-formats
    python -m egernia_bench full
    python -m egernia_bench report [<run-dir>]
    python -m egernia_bench serialize            # writers only, no cluster

Every command that measures anything accepts --resume <run-dir> and continues
where it stopped.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import time

from . import cluster
from . import corpus as corpus_mod
from . import runs as runs_mod
from . import serialize as serialize_mod
from .analyze import html as html_mod
from .analyze import report as report_mod
from .orchestrate import runner


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_corpus(cfg: dict, datasets: dict) -> list:
    """One corpus for the whole run, sized to the *smallest* dataset in it.

    Sized to the smallest, and built once, for the same reason: a size sweep
    has to issue the identical workload at every size or it is not measuring
    size. Rebuilding per dataset — which this did — gave each tier a different
    set of queries, so "throughput versus database size" compared different
    workloads and the recorded corpus hash described only the last one.

    The smallest works for all of them because generation is a prefix: a
    database grown to 25 GiB contains every row the 2 GiB one had, with the
    same identifiers at the same coordinates. So every lookup and every cone
    centre in the corpus exists at every size, and what changes between tiers
    is how much *other* data surrounds it — which is the thing being measured.
    Sizing to the largest instead would make most identifiers absent from the
    smaller tiers and turn the sweep into a comparison of miss rates.
    """
    counts = [
        info.get("observations_generated") or 0
        for info in datasets.values()
        if info.get("observations_generated")
    ]
    return corpus_mod.build(cfg["scenarios"], cfg["datasets"], min(counts, default=1))


def finalise(
    run,
    cfg: dict,
    results: list[dict],
    datasets: dict,
    digests: dict,
    keda: list[dict] | None = None,
    plan_flags: dict | None = None,
    corpus_entries: list | None = None,
) -> pathlib.Path:
    """Write summary.json/csv, draw the plots, render the report."""

    # The autoscaling scenarios count here too. They are measurements with a
    # classification like any other, and leaving them out made the tally of a
    # KEDA run a tally of its warm-up sweep.
    tally: dict[str, dict] = {}
    for result in [*results, *(keda or [])]:
        for verdict in result.get("bottleneck") or []:
            entry = tally.setdefault(
                verdict["classification"], {"count": 0, "explanation": verdict["explanation"]}
            )
            entry["count"] += 1

    # Per-class aggregation across every measurement, weighted by nothing: it
    # is a pooled view, and the report says so rather than implying these are
    # independent capacities.
    pooled: dict[str, dict] = {}
    for result in results:
        for cls, summary in (result.get("by_class") or {}).items():
            existing = pooled.setdefault(cls, {"requests": 0, "rps": 0.0, "latency": {}})
            existing["requests"] += summary.get("requests") or 0
            existing["rps"] += summary.get("rps") or 0.0
            for field in ("p95_s", "p99_s", "p50_s"):
                value = (summary.get("latency") or {}).get(field)
                if value is not None:
                    existing["latency"][field] = max(existing["latency"].get(field, 0.0), value)

    headline = {}
    single = [r for r in results if r.get("kind") in ("concurrency", "saturation")]
    if single:
        best = max(single, key=lambda r: (r.get("http") or {}).get("rps") or 0.0)
        headline["peak single-replica throughput"] = {
            "value": round((best["http"]["rps"] or 0.0), 1),
            "evidence": f"{best['key']} at {best['concurrency']} clients, "
            f"p95 {1000 * best['http']['latency']['p95_s']:.0f} ms",
        }
    slo_p95_s = cfg["scenarios"]["slo"]["p95_seconds"]
    c1 = runner.sustainable_capacity(results, slo_p95_s)
    signals_required = runner.saturation_signals_required(cfg)
    headline.update(runner.capacity_headline(results, slo_p95_s, signals_required))

    invalid_path = run.path / "invalid.json"
    summary = {
        "scenario": run.scenario,
        "generated_at": time.time(),
        "environment": runs_mod.environment(
            cluster.versions(),
            digests,
            cfg["hardware"],
            cfg["datasets"]["generation"]["seed"],
            corpus_mod.corpus_hash(corpus_entries or []),
            cfg["chart_values_text"],
        ),
        "datasets": datasets,
        "runs": results,
        "keda": keda or [],
        "bottleneck_tally": tally,
        "by_query_class": pooled,
        "headline": headline,
        "replica_capacity": runner.replica_capacities(results, slo_p95_s, signals_required),
        "format_comparison": runner.format_comparison(results),
        "shedding": runner.shedding_summary(results),
        "plan_flags": {
            name: {"count": count, "explanation": _plan_explanation(name)}
            for name, count in (plan_flags or {}).items()
        },
        "invalid": json.loads(invalid_path.read_text())["reasons"] if invalid_path.exists() else [],
        "sustainable_capacity_c1": c1,
    }
    run.write_json("summary.json", summary)
    run.write_json("environment.json", summary["environment"])
    run.write_json("dataset.json", datasets)
    html_mod.write_csv(summary, run.path / "summary.csv")

    plotter = report_mod.Plotter(run.path, summary)
    figures = plotter.draw_all()
    report = html_mod.render(run.path, summary, figures)
    logging.getLogger("egernia_bench").info("report written to %s", report)
    return report


PLAN_EXPLANATIONS = {
    "sequential_scan_on_large_table": (
        "A point or cone query scanned a whole large table. Either an index is "
        "missing or the planner did not think it was worth using."
    ),
    "large_rows_removed_by_filter": (
        "The plan fetched rows only to throw them away - work done for nothing, "
        "and usually a missing or wrong-ordered index."
    ),
    "temporary_spill": "A sort or hash exceeded work_mem and went to disk.",
    "bad_cardinality_estimate": (
        "The planner's row estimate was an order of magnitude out, which is how "
        "good indexes get ignored."
    ),
    "large_nested_loop": (
        "A nested loop executed its inner side thousands of times; usually a "
        "cardinality misestimate upstream."
    ),
    "high_io_time": "Most of the query's time was spent waiting for the disk.",
    "expected_index_unused": (
        "The index this query class exists to exercise was not used. This is the "
        "flag most likely to be a real defect rather than a tuning note."
    ),
}


def _plan_explanation(flag: str) -> str:
    return PLAN_EXPLANATIONS.get(flag, "")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_smoke(args) -> int:
    """A short end-to-end pass: everything wired, nothing measured for long."""
    cfg = runner.load_config()
    run = runs_mod.new_run("smoke", args.resume)
    digests = runner.setup(cfg, rebuild_images=not args.no_build)
    datasets = runner.ensure_dataset(cfg, ["D1"], run.path / "datasets")
    entries = build_corpus(cfg, datasets)
    run.write_json("corpus.json", [e.as_dict() for e in entries[:200]])
    results = runner.concurrency_sweep(run, cfg, "D1", entries, quick=True)
    plan_flags = runner.capture_plans(run, cfg, entries)
    finalise(run, cfg, results, datasets, digests, plan_flags=plan_flags, corpus_entries=entries)
    return 0


def cmd_db_scaling(args) -> int:
    cfg = runner.load_config()
    run = runs_mod.new_run("db-scaling", args.resume)
    digests = runner.setup(cfg, rebuild_images=not args.no_build)
    names = args.datasets or [d["name"] for d in cfg["datasets"]["datasets"]]
    results: list[dict] = []
    datasets: dict = {}
    plan_flags: dict = {}
    entries: list = []
    for name in names:
        # Grown in order: the database is one database, and each tier is the
        # previous one with more rows in it.
        datasets |= runner.ensure_dataset(cfg, [name], run.path / "datasets")
        if not entries:
            # Built once, from the first and therefore smallest tier, so every
            # dataset in this run is measured with the identical workload.
            entries = build_corpus(cfg, datasets)
            run.write_json("corpus.json", [e.as_dict() for e in entries])
        results += runner.concurrency_sweep(run, cfg, name, entries, quick=args.quick)
        results += runner.stress_classes(run, cfg, name, entries)
        plan_flags = runner.capture_plans(run, cfg, entries)
        run.write_json(f"plan-flags-{name}.json", plan_flags)
    finalise(run, cfg, results, datasets, digests, plan_flags=plan_flags, corpus_entries=entries)
    return 0


def cmd_fixed_scaling(args) -> int:
    cfg = runner.load_config()
    run = runs_mod.new_run("fixed-scaling", args.resume)
    digests = runner.setup(cfg, rebuild_images=not args.no_build)
    dataset = args.dataset or cfg["scenarios"]["fixed_replica_scaling"]["dataset"]
    datasets = runner.ensure_dataset(cfg, [dataset], run.path / "datasets")
    entries = build_corpus(cfg, datasets)
    # The sweep exists to find C1. Given one, skip it: re-measuring a ladder
    # that has already been measured is an hour and a half of the cluster's
    # time spent producing a number the caller already has.
    results: list[dict] = []
    c1 = args.c1
    if not c1:
        results = runner.concurrency_sweep(run, cfg, dataset, entries, quick=args.quick)
        c1 = runner.sustainable_capacity(results, cfg["scenarios"]["slo"]["p95_seconds"])
    if not c1:
        # Without C1 every offered rate in this family is meaningless, so this
        # stops rather than inventing one.
        print("could not establish C1: no single-replica measurement met the SLO", file=sys.stderr)
        return 2
    run.write_json("c1.json", {"c1_rps": c1})
    results += runner.fixed_replica_scaling(run, cfg, dataset, entries, c1)
    finalise(run, cfg, results, datasets, digests, corpus_entries=entries)
    return 0


def cmd_keda(args) -> int:
    cfg = runner.load_config()
    run = runs_mod.new_run("keda", args.resume)
    digests = runner.setup(cfg, rebuild_images=not args.no_build)
    dataset = args.dataset or "D1"
    datasets = runner.ensure_dataset(cfg, [dataset], run.path / "datasets")
    entries = build_corpus(cfg, datasets)
    c1 = args.c1
    results: list[dict] = []
    if not c1:
        results = runner.concurrency_sweep(run, cfg, dataset, entries, quick=True)
        c1 = runner.sustainable_capacity(results, cfg["scenarios"]["slo"]["p95_seconds"])
    if not c1:
        print("could not establish C1", file=sys.stderr)
        return 2
    # Async capacity is not sync capacity: one executor runs one query at a
    # time, so the job rate a single executor sustains is far below the request
    # rate an API replica sustains. Scaled here rather than reused blindly.
    async_c1 = args.async_c1 or max(0.5, c1 / 20.0)
    run.write_json("c1.json", {"c1_rps": c1, "async_c1_jobs_per_s": async_c1})
    keda = runner.keda_scenarios(run, cfg, dataset, entries, async_c1, only=args.scenarios)
    finalise(run, cfg, results, datasets, digests, keda=keda, corpus_entries=entries)
    return 0


def cmd_result_formats(args) -> int:
    """Package 10: every writer, the same rows.

    No concurrency sweep first. This family does not need C1 — it holds the
    load fixed on purpose, because the question is what a large result costs
    to produce and not where the service gives up producing them.
    """
    cfg = runner.load_config()
    run = runs_mod.new_run("result-formats", args.resume)
    digests = runner.setup(cfg, rebuild_images=not args.no_build)
    dataset = args.dataset or cfg["scenarios"]["result_formats"]["dataset"]
    datasets = runner.ensure_dataset(cfg, [dataset], run.path / "datasets")
    entries = build_corpus(cfg, datasets)
    run.write_json("corpus.json", [e.as_dict() for e in entries])
    results = runner.result_formats(run, cfg, dataset, entries)
    # The writers on their own, beside the same writers behind an HTTP
    # request: which of the two moved is the whole question when a change to
    # the serialisation path does not show up end to end.
    run.write_json("serialization.json", serialize_mod.report())
    finalise(run, cfg, results, datasets, digests, corpus_entries=entries)
    return 0


def cmd_stress(args) -> int:
    """The stress classes (Q09, Q11, Q13, Q14) on their own, nothing else.

    The full families measure these too, buried after a concurrency sweep;
    this runs just them, so a change aimed at one expensive class (package
    11's full-table aggregate, say) can be measured in minutes instead of
    re-running a whole family for four numbers.
    """
    cfg = runner.load_config()
    run = runs_mod.new_run("stress", args.resume)
    digests = runner.setup(cfg, rebuild_images=not args.no_build)
    dataset = args.dataset or "D1"
    datasets = runner.ensure_dataset(cfg, [dataset], run.path / "datasets")
    entries = build_corpus(cfg, datasets)
    run.write_json("corpus.json", [e.as_dict() for e in entries])
    results = runner.stress_classes(run, cfg, dataset, entries)
    plan_flags = runner.capture_plans(run, cfg, entries)
    finalise(run, cfg, results, datasets, digests, plan_flags=plan_flags, corpus_entries=entries)
    return 0


def cmd_replicas(args) -> int:
    """Package 14: a bracketed capacity per replica count.

    The efficiency column only populates from ceilings, and the open-loop
    rungs never measured any: this is the bounded-concurrency shape that
    does.
    """
    cfg = runner.load_config()
    run = runs_mod.new_run("replica-sweep", args.resume)
    digests = runner.setup(cfg, rebuild_images=not args.no_build)
    dataset = args.dataset or "D1"
    datasets = runner.ensure_dataset(cfg, [dataset], run.path / "datasets")
    entries = build_corpus(cfg, datasets)
    run.write_json("corpus.json", [e.as_dict() for e in entries])
    results = runner.replica_sweep(run, cfg, dataset, entries)
    finalise(run, cfg, results, datasets, digests, corpus_entries=entries)
    slo = cfg["scenarios"]["slo"]["p95_seconds"]
    for row in runner.replica_capacities(results, slo, runner.saturation_signals_required(cfg)):
        print(
            f"n={row['replicas']}: {row['rps']:.1f} rps"
            f" ({'ceiling' if row['bracketed'] else 'open-ended'}, {row['key']})"
        )
    return 0


def cmd_shedding(args) -> int:
    """Package 13: hold a bounded-concurrency overload, watch the refusals.

    Prints the reduction the package asked for — per held concurrency, how
    much of the shed load was answered (503) and how much was dropped at the
    transport (see runner.TRANSPORT_DROP_ERRORS) — with and without
    `tapApi.limitConcurrency`.
    """
    cfg = runner.load_config()
    run = runs_mod.new_run("shedding", args.resume)
    digests = runner.setup(cfg, rebuild_images=not args.no_build)
    dataset = args.dataset or cfg["scenarios"]["shedding"]["dataset"]
    datasets = runner.ensure_dataset(cfg, [dataset], run.path / "datasets")
    entries = build_corpus(cfg, datasets)
    run.write_json("corpus.json", [e.as_dict() for e in entries])
    results = runner.shedding(run, cfg, dataset, entries)
    finalise(run, cfg, results, datasets, digests, corpus_entries=entries)
    for row in runner.shedding_summary(results):
        print(
            f"{row['key']:32} {row['requests']:7d} requests  "
            f"{row['rps']:7.1f} rps  503={row['refused_503']:<7d} "
            f"drops={row['transport_drops']:<7d} other={row['other_errors']}"
        )
    return 0


def cmd_serialize(args) -> int:
    """The writers on their own: no cluster, no database, no HTTP."""
    payload = serialize_mod.report(
        row_counts=args.rows, repetitions=args.repetitions, seed=args.seed
    )
    print(serialize_mod.table(payload["measurements"]))
    if args.out:
        print(f"\nwritten to {serialize_mod.write(payload, pathlib.Path(args.out))}")
    return 0


def cmd_full(args) -> int:
    cfg = runner.load_config()
    run = runs_mod.new_run("full", args.resume)
    digests = runner.setup(cfg, rebuild_images=not args.no_build)
    names = args.datasets or [d["name"] for d in cfg["datasets"]["datasets"]]
    results: list[dict] = []
    datasets: dict = {}
    entries: list = []
    plan_flags: dict = {}
    for name in names:
        datasets |= runner.ensure_dataset(cfg, [name], run.path / "datasets")
        if not entries:
            entries = build_corpus(cfg, datasets)
        results += runner.concurrency_sweep(run, cfg, name, entries)
        results += runner.stress_classes(run, cfg, name, entries)
        plan_flags = runner.capture_plans(run, cfg, entries)
    results += runner.result_formats(
        run, cfg, cfg["scenarios"]["result_formats"]["dataset"], entries
    )
    run.write_json("serialization.json", serialize_mod.report())
    c1 = runner.sustainable_capacity(results, cfg["scenarios"]["slo"]["p95_seconds"])
    keda: list[dict] = []
    if c1:
        run.write_json("c1.json", {"c1_rps": c1})
        biggest = names[-1]
        results += runner.fixed_replica_scaling(run, cfg, biggest, entries, c1)
        keda = runner.keda_scenarios(run, cfg, biggest, entries, max(0.5, c1 / 20.0))
    finalise(
        run,
        cfg,
        results,
        datasets,
        digests,
        keda=keda,
        plan_flags=plan_flags,
        corpus_entries=entries,
    )
    return 0


def cmd_report(args) -> int:
    path = runs_mod.resolve(args.run_dir)
    if not path:
        print("no run directory found", file=sys.stderr)
        return 2
    summary = html_mod.load_summary(path)
    plotter = report_mod.Plotter(path, summary)
    figures = plotter.draw_all()
    html_mod.write_csv(summary, path / "summary.csv")
    print(html_mod.render(path, summary, figures))
    return 0


def cmd_publish(args) -> int:
    """Copy a run's graphs and numbers into the docs site."""
    from .analyze import publish as publish_mod

    path = runs_mod.resolve(args.run_dir)
    if not path:
        print("no run directory found", file=sys.stderr)
        return 2
    if not (path / "summary.json").exists():
        print(f"{path} has no summary.json; run `report` first", file=sys.stderr)
        return 2
    print(publish_mod.publish(path))
    return 0


def cmd_reclassify(args) -> int:
    """Re-derive a finished run's analysis from its stored artefacts."""
    path = runs_mod.resolve(args.run_dir)
    if not path:
        print("no run directory found", file=sys.stderr)
        return 2
    summary = runner.reclassify(path, runner.load_config())
    plotter = report_mod.Plotter(path, summary)
    figures = plotter.draw_all()
    html_mod.write_csv(summary, path / "summary.csv")
    print(html_mod.render(path, summary, figures))
    return 0


def cmd_setup(args) -> int:
    runner.setup(runner.load_config(), rebuild_images=not args.no_build)
    return 0


def cmd_teardown(args) -> int:
    cluster.teardown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="egernia_bench", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, **kwargs):
        sub = subparsers.add_parser(name, **kwargs)
        sub.set_defaults(handler=handler)
        sub.add_argument("--resume", help="continue an existing results directory")
        sub.add_argument(
            "--no-build",
            action="store_true",
            help="skip rebuilding images (they must already be loaded)",
        )
        return sub

    add("smoke", cmd_smoke, help="short end-to-end pass on D1")
    sub = add("db-scaling", cmd_db_scaling, help="concurrency sweep per dataset")
    sub.add_argument("--datasets", nargs="+")
    sub.add_argument("--quick", action="store_true")
    sub = add("fixed-scaling", cmd_fixed_scaling, help="replica scaling with autoscalers off")
    sub.add_argument("--dataset")
    sub.add_argument("--c1", type=float)
    sub.add_argument("--quick", action="store_true")
    sub = add("result-formats", cmd_result_formats, help="every result writer over the same rows")
    sub.add_argument("--dataset")
    sub = add("stress", cmd_stress, help="just the stress classes (Q09, Q11, Q13, Q14)")
    sub.add_argument("--dataset")
    sub = add("shedding", cmd_shedding, help="held overload: 503s versus socket drops")
    sub.add_argument("--dataset")
    sub = add("replicas", cmd_replicas, help="a bracketed capacity per replica count")
    sub.add_argument("--dataset")
    sub = add("keda", cmd_keda, help="autoscaling scenarios K1-K7")
    sub.add_argument("--dataset")
    sub.add_argument("--c1", type=float)
    sub.add_argument("--async-c1", type=float, help="sustainable jobs/second for one executor")
    sub.add_argument("--scenarios", nargs="+", help="subset, e.g. K1 K3")
    sub = add("full", cmd_full, help="every family, every dataset")
    sub.add_argument("--datasets", nargs="+")
    sub = subparsers.add_parser("report", help="redraw plots and HTML for a run")
    sub.set_defaults(handler=cmd_report)
    sub.add_argument("run_dir", nargs="?")
    sub = subparsers.add_parser(
        "publish", help="copy a run's graphs and numbers into the docs site"
    )
    sub.set_defaults(handler=cmd_publish)
    sub.add_argument("run_dir", nargs="?")
    sub = subparsers.add_parser(
        "reclassify",
        help="re-derive a finished run's database summary and bottleneck verdicts",
    )
    sub.set_defaults(handler=cmd_reclassify)
    sub.add_argument("run_dir", nargs="?")
    sub = subparsers.add_parser(
        "serialize", help="per-row cost of each result writer, in process, no cluster"
    )
    sub.set_defaults(handler=cmd_serialize)
    sub.add_argument("--rows", nargs="+", type=int, default=[1000, 10000])
    sub.add_argument("--repetitions", type=int, default=15)
    sub.add_argument("--seed", type=int, default=20260823)
    sub.add_argument("--out", help="also write the measurements as JSON to this path")
    sub = subparsers.add_parser("setup", help="cluster, KEDA, monitoring, chart")
    sub.set_defaults(handler=cmd_setup)
    sub.add_argument("--no-build", action="store_true")
    sub = subparsers.add_parser("teardown", help="delete the kind cluster")
    sub.set_defaults(handler=cmd_teardown)

    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
