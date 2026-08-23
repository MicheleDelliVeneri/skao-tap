"""Command line for the benchmark suite.

    python -m tapbench smoke
    python -m tapbench db-scaling
    python -m tapbench fixed-scaling
    python -m tapbench keda
    python -m tapbench full
    python -m tapbench report [<run-dir>]

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
    """The corpus, sized to the largest dataset actually built.

    Parameters are drawn from the observation index space, so the corpus has to
    know how far that space goes — a corpus built for 8 million observations
    against a database holding 300,000 would spend most of its lookups missing.
    """
    observations = max(
        (info.get("observations_generated") or 0 for info in datasets.values()),
        default=1,
    )
    return corpus_mod.build(cfg["scenarios"], cfg["datasets"], observations)


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

    tally: dict[str, dict] = {}
    for result in results:
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
    c1 = runner.sustainable_capacity(results, cfg["scenarios"]["slo"]["p95_seconds"])
    if c1:
        headline["sustainable single-replica capacity (C1)"] = {
            "value": round(c1, 1),
            "evidence": f"highest successful rps with p95 within the "
            f"{cfg['scenarios']['slo']['p95_seconds']}s SLO and "
            "errors under 1%",
        }
    for kind, label in (("fixed_replicas", "replica scaling efficiency at 8"),):
        scaled = [r for r in results if r.get("kind") == kind]
        base = [r for r in scaled if r.get("replicas") == 1]
        top = [r for r in scaled if r.get("replicas") == 8]
        if base and top:
            one = max((r["http"]["successful_rps"] or 0.0) for r in base)
            eight = max((r["http"]["successful_rps"] or 0.0) for r in top)
            if one:
                headline[label] = {
                    "value": round(eight / (8 * one), 3),
                    "evidence": f"{eight:.1f} rps on 8 replicas against {one:.1f} on one",
                }

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
    logging.getLogger("tapbench").info("report written to %s", report)
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
        entries = build_corpus(cfg, datasets)
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
        entries = build_corpus(cfg, datasets)
        results += runner.concurrency_sweep(run, cfg, name, entries)
        results += runner.stress_classes(run, cfg, name, entries)
        plan_flags = runner.capture_plans(run, cfg, entries)
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


def cmd_setup(args) -> int:
    runner.setup(runner.load_config(), rebuild_images=not args.no_build)
    return 0


def cmd_teardown(args) -> int:
    cluster.teardown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tapbench", description=__doc__)
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
