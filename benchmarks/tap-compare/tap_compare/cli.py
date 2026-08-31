"""The tap-compare command line.

    uv run --group tap-compare python benchmarks/tap-compare run \
        --target egernia-local --scenario smoke

One `run` executes one scenario against one target and writes a run
directory under benchmarks/tap-compare/results/: per-rung Parquet samples,
per-rung summaries with confidence intervals, and the environment
provenance. Comparing servers is running the same scenario against each
target (interleaving repetitions is the ladder loop's job in a later
phase; for now one target per invocation keeps the moving parts visible).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import pathlib
import sys

import yaml

from . import corpus as corpus_mod
from . import runner, runs, stats, targets

log = logging.getLogger("tap_compare")

SUITE = pathlib.Path(__file__).resolve().parents[1]
REPO = SUITE.parents[1]
DATASET_CONFIG = REPO / "dataset" / "config" / "datasets.yaml"


def _load_yaml(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


def _build_corpus(cfg: dict, portable_only: bool) -> list[corpus_mod.CorpusEntry]:
    dataset_cfg = _load_yaml(DATASET_CONFIG)
    return corpus_mod.build(
        cfg, dataset_cfg, cfg["corpus"]["projects"], portable_only=portable_only
    )


def _rung(
    target: targets.Target,
    entries: list[corpus_mod.CorpusEntry],
    scenario: dict,
    mix: dict[str, float] | None,
    query_class: str | None,
    concurrency: int,
    repetition: int,
    response_format: str,
    seed: int,
) -> tuple[runner.Recorder, float]:
    return runner.closed_loop_sharded(
        target.base_url,
        entries=entries,
        mix=mix,
        query_class=query_class,
        seed=seed + repetition * 10_000,
        concurrency=concurrency,
        warmup_s=scenario["warmup_seconds"],
        measure_s=scenario["measure_seconds"],
        processes=scenario.get("generator_processes", 1),
        response_format=response_format,
        maxrec=scenario.get("maxrec"),
    )


def _saturated(summaries: list[dict], scenario: dict) -> bool:
    """Whether the ladder has hit enough agreeing signals to stop climbing."""
    signals_cfg = scenario.get("signals")
    required = scenario.get("saturation_signals_required")
    if not signals_cfg or not required or len(summaries) < 2:
        return False
    verdict = stats.saturation_signals(
        summaries[-1], summaries[0], summaries[-2], resources={}, thresholds=signals_cfg
    )
    if verdict["count"] >= required:
        log.info("saturation signals tripped: %s", ", ".join(verdict["tripped"]))
        return True
    return False


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_yaml(SUITE / "config" / "scenarios.yaml")
    scenario = cfg["scenarios"][args.scenario]
    all_targets = targets.load()
    if args.target not in all_targets:
        raise SystemExit(f"unknown target {args.target!r} (one of: {', '.join(all_targets)})")
    target = all_targets[args.target]
    if args.base_url:
        # a local port override, not a different target: everything else the
        # descriptor says (server, portability, notes) still applies
        target = dataclasses.replace(target, base_url=args.base_url.rstrip("/"))

    entries = _build_corpus(cfg, target.portable_only)
    corpus_sha = corpus_mod.corpus_hash(entries)
    mix = {k: float(v) for k, v in cfg["mix"].items()}

    run = runs.new_run(f"{args.scenario}-{target.name}", resume=args.resume)
    run.write_json(
        "environment.json",
        runs.environment(
            target.as_dict(),
            seed=cfg["corpus"]["seed"],
            corpus_sha256=corpus_sha,
            extras={"scenario": {args.scenario: scenario}},
        ),
    )
    run.write_json("corpus.json", [e.as_dict() for e in entries])

    classes = sorted({e.query_class for e in entries}) if scenario.get("per_class") else [None]
    guard_max = cfg["guards"]["generator_cpu_max_fraction"]
    rows: list[dict] = []
    for response_format in scenario["response_formats"]:
        for query_class in classes:
            summaries: list[dict] = []
            for concurrency in scenario["ladder"]:
                for repetition in range(1, scenario["repetitions"] + 1):
                    key = f"{response_format}-{query_class or 'mix'}-c{concurrency}-r{repetition}"
                    if run.done(key):
                        log.info("skipping %s (already done)", key)
                        continue
                    recorder, elapsed = _rung(
                        target,
                        entries,
                        scenario,
                        None if query_class else mix,
                        query_class,
                        concurrency,
                        repetition,
                        response_format,
                        cfg["corpus"]["seed"],
                    )
                    runner.write_samples(recorder.samples, run.samples_dir / f"{key}.parquet")
                    summary = stats.summarise(recorder.samples, elapsed)
                    summary.update(
                        target=target.name,
                        server=target.server,
                        response_format=response_format,
                        query_class=query_class or "mix",
                        concurrency=concurrency,
                        repetition=repetition,
                        generator_cpu_peak=recorder.generator_cpu_peak,
                        generator_guard_ok=recorder.generator_cpu_peak < guard_max,
                        by_class=stats.by_query_class(recorder.samples, elapsed),
                    )
                    if recorder.generator_cpu_peak >= guard_max:
                        run.invalidate(
                            "generator ran hot",
                            {"key": key, "generator_cpu_peak": recorder.generator_cpu_peak},
                        )
                    rows.append(summary)
                    run.write_json(f"summaries/{key}.json", summary)
                    run.mark_done(key, {"requests": summary["requests"]})
                    summaries.append(summary)
                if _saturated(summaries, scenario):
                    break
    run.write_json("summary.json", rows)
    log.info("run complete: %s (%d rung summaries)", run.path, len(rows))
    return 0


def _resolve_targets(names: list[str]) -> dict[str, targets.Target]:
    all_targets = targets.load()
    chosen = {}
    for name in names:
        if name not in all_targets:
            raise SystemExit(f"unknown target {name!r} (one of: {', '.join(all_targets)})")
        chosen[name] = all_targets[name]
    return chosen


def _run_gates(run: runs.Run, chosen: dict, entries: list, maxrec: int) -> dict:
    """VOSI provenance, taplint conformance, cross-server agreement.

    No timed rung may be compared across servers unless this passed for all
    of them on the same corpus.
    """
    from . import conformance, validate, vosi

    outcome: dict = {"targets": {}}
    for name, target in chosen.items():
        facts = vosi.capture(target.base_url, run.path / "capabilities" / name)
        log.info(
            "%s: TAP %s, %d formats, maxrec default %s",
            name,
            ",".join(facts["tap_versions"]) or "?",
            len(facts["output_formats"]),
            facts["maxrec_default"],
        )
        lint = conformance.run_taplint(target.base_url, run.path / "taplint" / f"{name}.txt")
        log.info(
            "%s: taplint %s (%d blocking / %d total errors)",
            name,
            "PASS" if lint["passed"] else "FAIL",
            lint["errors_blocking"],
            lint["errors_total"],
        )
        outcome["targets"][name] = {"vosi": facts, "taplint": lint}
    if len(chosen) > 1:
        verdict = validate.agreement(
            {name: t.base_url for name, t in chosen.items()}, entries, maxrec=maxrec
        )
        outcome["agreement"] = verdict
        log.info(
            "agreement: %d classes agree, disagreeing: %s",
            len(verdict["agreed"]),
            ", ".join(verdict["disagreed"]) or "none",
        )
    run.write_json("gates.json", outcome)
    return outcome


def cmd_gates(args: argparse.Namespace) -> int:
    cfg = _load_yaml(SUITE / "config" / "scenarios.yaml")
    chosen = _resolve_targets(args.targets)
    entries = _build_corpus(cfg, portable_only=True)
    run = runs.new_run("gates-" + "-".join(sorted(chosen)))
    outcome = _run_gates(run, chosen, entries, maxrec=cfg["scenarios"]["ladder"]["maxrec"])
    log.info("gates -> %s", run.path)
    failed = any(not t["taplint"]["passed"] for t in outcome["targets"].values())
    return 1 if failed else 0


def cmd_compare(args: argparse.Namespace) -> int:
    """One comparison run: gates, then interleaved rungs across targets.

    Repetitions are interleaved round-robin across servers (A,B,A,B — never
    AAABBB), so host drift decorrelates from server identity. Every target
    is held to the portable corpus and the same fixed rung grid; there is no
    early stop, because comparison cells have to align.
    """
    cfg = _load_yaml(SUITE / "config" / "scenarios.yaml")
    scenario = cfg["scenarios"][args.scenario]
    chosen = _resolve_targets(args.targets)
    entries = _build_corpus(cfg, portable_only=True)
    corpus_sha = corpus_mod.corpus_hash(entries)
    mix = {k: float(v) for k, v in cfg["mix"].items()}

    run = runs.new_run("tap-compare", resume=args.resume)
    run.write_json(
        "environment.json",
        runs.environment(
            {name: t.as_dict() for name, t in chosen.items()},
            seed=cfg["corpus"]["seed"],
            corpus_sha256=corpus_sha,
            extras={"scenario": {args.scenario: scenario}},
        ),
    )
    run.write_json("corpus.json", [e.as_dict() for e in entries])

    if run.done("gates"):
        gates = json.loads((run.path / "gates.json").read_text())
    else:
        gates = _run_gates(run, chosen, entries, maxrec=scenario["maxrec"])
        run.mark_done("gates")
    failed = [n for n, t in gates["targets"].items() if not t["taplint"]["passed"]]
    if failed and not args.allow_gate_failures:
        raise SystemExit(f"taplint failed for {', '.join(failed)}; refusing to compare")
    excluded = set(gates.get("agreement", {}).get("disagreed", []))

    classes: list[str | None] = [None]  # None = the mixed workload
    if scenario.get("per_class"):
        classes += [c for c in sorted({e.query_class for e in entries}) if c not in excluded]

    guard_max = cfg["guards"]["generator_cpu_max_fraction"]
    rows: list[dict] = []
    for response_format in scenario["response_formats"]:
        for query_class in classes:
            for concurrency in scenario["ladder"]:
                for repetition in range(1, scenario["repetitions"] + 1):
                    for name, target in chosen.items():
                        key = (
                            f"{name}-{response_format}-{query_class or 'mix'}"
                            f"-c{concurrency}-r{repetition}"
                        )
                        summary_path = run.path / "summaries" / f"{key}.json"
                        if run.done(key):
                            rows.append(json.loads(summary_path.read_text()))
                            continue
                        recorder, elapsed = _rung(
                            target,
                            entries,
                            scenario,
                            None if query_class else mix,
                            query_class,
                            concurrency,
                            repetition,
                            response_format,
                            cfg["corpus"]["seed"],
                        )
                        runner.write_samples(recorder.samples, run.samples_dir / f"{key}.parquet")
                        summary = stats.summarise(recorder.samples, elapsed)
                        summary.update(
                            target=name,
                            server=target.server,
                            response_format=response_format,
                            query_class=query_class or "mix",
                            concurrency=concurrency,
                            repetition=repetition,
                            generator_cpu_peak=recorder.generator_cpu_peak,
                            generator_guard_ok=recorder.generator_cpu_peak < guard_max,
                        )
                        if recorder.generator_cpu_peak >= guard_max:
                            run.invalidate(
                                "generator ran hot",
                                {"key": key, "peak": recorder.generator_cpu_peak},
                            )
                        rows.append(summary)
                        run.write_json(f"summaries/{key}.json", summary)
                        run.mark_done(key, {"requests": summary["requests"]})
    run.write_json("summary.json", rows)
    log.info("comparison complete: %s (%d rung summaries)", run.path, len(rows))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """Render a comparison run into docs/performance/."""
    from . import publish

    run_dir = runs.RESULTS / args.run
    if not run_dir.is_dir():
        raise SystemExit(f"no such run: {run_dir}")
    out_dir = pathlib.Path(args.out) if args.out else REPO / "docs" / "performance" / run_dir.name
    page = publish.render(run_dir, out_dir)
    log.info("published %s", page)
    return 0


def cmd_corpus(args: argparse.Namespace) -> int:
    """Print the corpus (for inspection and for the future agreement gate)."""
    cfg = _load_yaml(SUITE / "config" / "scenarios.yaml")
    entries = _build_corpus(cfg, portable_only=not args.all_classes)
    for entry in entries:
        print(f"{entry.query_class}\t{entry.query_id}\t{entry.adql}")
    print(f"# {len(entries)} entries, sha256 {corpus_mod.corpus_hash(entries)}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # one line per rung, not one per request
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="tap-compare")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="run one scenario against one target")
    run_parser.add_argument("--target", required=True)
    run_parser.add_argument("--scenario", default="smoke")
    run_parser.add_argument("--resume", help="existing run directory name to continue")
    run_parser.add_argument("--base-url", help="override the target's TAP root (local ports)")
    run_parser.set_defaults(func=cmd_run)

    gates_parser = sub.add_parser(
        "gates", help="VOSI provenance + taplint conformance + cross-server agreement"
    )
    gates_parser.add_argument("--targets", nargs="+", required=True)
    gates_parser.set_defaults(func=cmd_gates)

    compare_parser = sub.add_parser(
        "compare", help="gates, then interleaved comparison rungs across targets"
    )
    compare_parser.add_argument("--targets", nargs="+", required=True)
    compare_parser.add_argument("--scenario", default="compare-demo")
    compare_parser.add_argument("--resume", help="existing run directory name to continue")
    compare_parser.add_argument(
        "--allow-gate-failures",
        action="store_true",
        help="measure anyway (debugging only; the report will say the gate failed)",
    )
    compare_parser.set_defaults(func=cmd_compare)

    publish_parser = sub.add_parser("publish", help="render a comparison run into docs/performance")
    publish_parser.add_argument("--run", required=True, help="run directory name under results/")
    publish_parser.add_argument("--out", help="output directory (default docs/performance/<run>)")
    publish_parser.set_defaults(func=cmd_publish)

    corpus_parser = sub.add_parser("corpus", help="print the deterministic query corpus")
    corpus_parser.add_argument("--all-classes", action="store_true")
    corpus_parser.set_defaults(func=cmd_corpus)

    args = parser.parse_args(argv)
    return args.func(args)
