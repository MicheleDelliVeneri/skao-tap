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
import re
import sys
import time

import yaml

from . import corpus as corpus_mod
from . import runner, runs, stats, targets

log = logging.getLogger("tap_compare")

SUITE = pathlib.Path(__file__).resolve().parents[1]
REPO = SUITE.parents[1]
DATASET_CONFIG = REPO / "dataset" / "config" / "datasets.yaml"


def _load_yaml(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


def _config(args: argparse.Namespace) -> tuple[dict, dict[str, targets.Target]]:
    """The scenarios and targets of one config directory (default: config/).

    A protocol is a directory: another protocol (the resource-scaling one
    under scaling/) is another directory, never an edit to a frozen one.
    """
    config_dir = pathlib.Path(args.config_dir)
    return _load_yaml(config_dir / "scenarios.yaml"), targets.load(config_dir / "targets.yaml")


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


def _record_provenance(
    run: runs.Run,
    target: dict,
    cfg: dict,
    corpus_sha: str,
    scenario_name: str,
    scenario: dict,
    entries: list[corpus_mod.CorpusEntry],
    tier: str | None = None,
) -> None:
    """Write environment.json and corpus.json once, at the start of a run.

    A resumed run keeps the provenance it started with: overwriting it would
    silently re-describe rungs that were measured under the original record.
    Resuming with a different corpus, scenario or target set is refused —
    those cells would not be comparable with the ones already on disk. A
    resume from a different git state is recorded (appended, never rewriting
    the original) rather than refused: refusing would discard a day-long run
    over a harness fix, but the record must say the code changed mid-run.
    A run is either tiered (``--tier``, prefixed rungs and gate records) or
    flat, never both: the mode is recorded at creation (inferred from the
    rungs on disk for runs that predate the record) and a resume in the other
    mode is refused.
    """
    mode = "tiered" if tier else "flat"
    env_path = run.path / "environment.json"
    if env_path.exists():
        recorded = json.loads(env_path.read_text())
        mismatches = []
        recorded_mode = recorded.get("tier_mode") or _tier_mode_on_disk(run.path) or mode
        if recorded_mode != mode:
            mismatches.append(
                f"it is a {recorded_mode} run"
                f" (tiers {', '.join(recorded.get('tiers') or []) or 'none recorded'})"
                f" and this invocation is {mode} (--tier {tier})"
            )
        if recorded["corpus_sha256"] != corpus_sha:
            mismatches.append(f"corpus {recorded['corpus_sha256'][:12]}… is now {corpus_sha[:12]}…")
        if recorded["scenario"] != {scenario_name: scenario}:
            mismatches.append(
                f"scenario {'/'.join(recorded['scenario'])} is now {scenario_name}"
                " (or its configuration changed)"
            )
        if recorded["target"] != target:
            mismatches.append("the target descriptors changed")
        if mismatches:
            raise SystemExit(f"cannot resume {run.path.name}: " + "; ".join(mismatches))
        changed = False
        if recorded.get("tier_mode") != recorded_mode:
            recorded["tier_mode"] = recorded_mode  # a run that predates the record
            changed = True
        if tier and tier not in (recorded.get("tiers") or []):
            recorded.setdefault("tiers", []).append(tier)
            changed = True
        current = {"sha": runs.git_sha(), "dirty": runs.git_dirty()}
        last = (recorded.get("resumed") or [recorded["git"]])[-1]
        if {"sha": last.get("sha"), "dirty": last.get("dirty")} != current:
            recorded.setdefault("resumed", []).append({"at": time.time(), **current})
            changed = True
            log.warning(
                "resuming under different code (git %s, dirty=%s); recorded in environment.json",
                current["sha"][:8],
                current["dirty"],
            )
        if changed:
            run.write_json("environment.json", recorded)
        return
    run.write_json(
        "environment.json",
        runs.environment(
            target,
            seed=cfg["corpus"]["seed"],
            corpus_sha256=corpus_sha,
            extras={
                "scenario": {scenario_name: scenario},
                "tier_mode": mode,
                **({"tiers": [tier]} if tier else {}),
            },
        ),
    )
    run.write_json("corpus.json", [e.as_dict() for e in entries])


TIER_LABEL = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _tier_label(value: str) -> str:
    """A --tier label goes into rung keys and file names: a closed alphabet."""
    if not TIER_LABEL.match(value):
        raise argparse.ArgumentTypeError(
            f"tier label {value!r} must match {TIER_LABEL.pattern} (it names files)"
        )
    return value


def _tier_mode_on_disk(run_path: pathlib.Path) -> str | None:
    """ "tiered" or "flat" from the rung markers of a run that predates the
    recorded mode; None when nothing has been measured yet."""
    keys = [p.stem for p in (run_path / "state").glob("*.done")]
    if not keys:
        return None
    return "tiered" if all(re.match(r"t[^-]+-", k) for k in keys) else "flat"


def cmd_run(args: argparse.Namespace) -> int:
    cfg, all_targets = _config(args)
    scenario = cfg["scenarios"][args.scenario]
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
    _record_provenance(run, target.as_dict(), cfg, corpus_sha, args.scenario, scenario, entries)

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


def _resolve_targets(
    names: list[str], all_targets: dict[str, targets.Target]
) -> dict[str, targets.Target]:
    chosen = {}
    for name in names:
        if name not in all_targets:
            raise SystemExit(f"unknown target {name!r} (one of: {', '.join(all_targets)})")
        chosen[name] = all_targets[name]
    return chosen


def _run_gates(run: runs.Run, chosen: dict, entries: list, maxrec: int, prefix: str = "") -> dict:
    """VOSI provenance, taplint conformance, cross-server agreement.

    No timed rung may be compared across servers unless this passed for all
    of them on the same corpus. ``prefix`` (a resource tier's ``t8-``) keeps
    one run's per-tier gate records apart.
    """
    from . import conformance, validate, vosi

    outcome: dict = {"targets": {}}
    for name, target in chosen.items():
        facts = vosi.capture(target.base_url, run.path / "capabilities" / f"{prefix}{name}")
        log.info(
            "%s: TAP %s, %d formats, maxrec default %s",
            name,
            ",".join(facts["tap_versions"]) or "?",
            len(facts["output_formats"]),
            facts["maxrec_default"],
        )
        lint = conformance.run_taplint(
            target.base_url, run.path / "taplint" / f"{prefix}{name}.txt"
        )
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
    run.write_json(f"{prefix}gates.json", outcome)
    return outcome


def cmd_gates(args: argparse.Namespace) -> int:
    cfg, all_targets = _config(args)
    chosen = _resolve_targets(args.targets, all_targets)
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

    A resource-scaling run measures the servers one at a time under one
    tier's pins (``--tier 16 --only egernia-local``): its rungs and gate
    records carry the tier as a ``t16-`` prefix, so one run directory holds
    every tier and the report renders one table per tier.
    """
    cfg, all_targets = _config(args)
    scenario = cfg["scenarios"][args.scenario]
    chosen = _resolve_targets(args.targets, all_targets)
    unknown = [n for n in args.only or [] if n not in chosen]
    if unknown:
        raise SystemExit(f"--only names targets outside --targets: {', '.join(unknown)}")
    entries = _build_corpus(cfg, portable_only=True)
    corpus_sha = corpus_mod.corpus_hash(entries)
    mix = {k: float(v) for k, v in cfg["mix"].items()}
    prefix = f"t{args.tier}-" if args.tier else ""

    run = runs.new_run("tap-compare-scaling" if args.tier else "tap-compare", resume=args.resume)
    _record_provenance(
        run,
        {name: t.as_dict() for name, t in chosen.items()},
        cfg,
        corpus_sha,
        args.scenario,
        scenario,
        entries,
        tier=args.tier,
    )

    if run.done(f"{prefix}gates"):
        gates = json.loads((run.path / f"{prefix}gates.json").read_text())
    else:
        gates = _run_gates(run, chosen, entries, maxrec=scenario["maxrec"], prefix=prefix)
        run.mark_done(f"{prefix}gates")
    failed = [n for n, t in gates["targets"].items() if not t["taplint"]["passed"]]
    if failed and not args.allow_gate_failures:
        raise SystemExit(f"taplint failed for {', '.join(failed)}; refusing to compare")
    if args.gates_only:
        return 1 if failed else 0
    excluded = set(gates.get("agreement", {}).get("disagreed", []))
    measured = {n: t for n, t in chosen.items() if not args.only or n in args.only}

    classes: list[str | None] = [None]  # None = the mixed workload
    if scenario.get("per_class"):
        classes += [c for c in sorted({e.query_class for e in entries}) if c not in excluded]

    guard_max = cfg["guards"]["generator_cpu_max_fraction"]
    for response_format in scenario["response_formats"]:
        for query_class in classes:
            for concurrency in scenario["ladder"]:
                for repetition in range(1, scenario["repetitions"] + 1):
                    for name, target in measured.items():
                        key = (
                            f"{prefix}{name}-{response_format}-{query_class or 'mix'}"
                            f"-c{concurrency}-r{repetition}"
                        )
                        if run.done(key):
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
                            **({"tier": args.tier} if args.tier else {}),
                        )
                        if recorder.generator_cpu_peak >= guard_max:
                            run.invalidate(
                                "generator ran hot",
                                {"key": key, "peak": recorder.generator_cpu_peak},
                            )
                        run.write_json(f"summaries/{key}.json", summary)
                        run.mark_done(key, {"requests": summary["requests"]})
    # every rung on disk, not just this invocation's: a resumed or per-tier
    # invocation must leave summary.json describing the whole run
    rows = [json.loads(p.read_text()) for p in sorted((run.path / "summaries").glob("*.json"))]
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
    cfg, _ = _config(args)
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
    parser.add_argument(
        "--config-dir",
        default=str(SUITE / "config"),
        help="directory holding scenarios.yaml and targets.yaml (default: config/)",
    )
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
    compare_parser.add_argument(
        "--tier",
        type=_tier_label,
        help="resource tier label (e.g. 8): prefixes this invocation's rungs and gate record",
    )
    compare_parser.add_argument(
        "--only",
        nargs="+",
        metavar="TARGET",
        help="measure only these targets' rungs (gates still cover every --targets)",
    )
    compare_parser.add_argument(
        "--gates-only", action="store_true", help="run (or reuse) the gates, then stop"
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
