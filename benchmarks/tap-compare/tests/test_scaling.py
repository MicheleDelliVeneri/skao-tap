"""The resource-scaling protocol: its pins follow the pre-registered sizing
rules, and the harness runs it as one run directory with per-tier records."""

import argparse
import json
import pathlib
import textwrap

import pytest
import yaml
from tap_compare import cli, publish, runner, runs

SUITE = pathlib.Path(__file__).resolve().parents[1]
SCALING = SUITE / "scaling"
TIERS = (8, 16, 24)
GIB = 2**30
POOL = 8  # connections per process (config.dbPoolMax)
PER_GATHER = 2

_UNITS = {"GB": GIB, "MB": 2**20, "g": GIB}


def _bytes(value: str) -> int:
    for suffix, scale in _UNITS.items():
        if value.endswith(suffix):
            return int(value.removesuffix(suffix)) * scale
    raise AssertionError(f"{value!r}: unknown unit")


def _services(name: str) -> dict:
    return yaml.safe_load((SCALING / "pins" / f"{name}.yml").read_text())["services"]


@pytest.mark.parametrize("tier", TIERS)
def test_egernia_pins_follow_the_sizing_rules(tier):
    services = _services(f"egernia-{tier}")
    assert {s["cpuset"] for s in services.values()} == {f"0-{tier - 1}"}
    memory = {name: _bytes(s["mem_limit"]) for name, s in services.items()}
    assert sum(memory.values()) == tier * GIB
    assert memory["db"] == tier * GIB // 2  # 1/2 db, 1/4 api, 1/4 executor
    flags = services["db"]["command"][1:]
    tuning = dict(kv.split("=", 1) for kv in flags[1::2])
    workers = int(services["tap-api"]["environment"]["TAP_API_WORKERS"])
    assert _bytes(tuning["shared_buffers"]) == memory["db"] // 4
    assert _bytes(tuning["effective_cache_size"]) == memory["db"] * 3 // 4
    pools = (workers + 1) * POOL  # API workers + one executor
    assert int(tuning["max_parallel_workers"]) >= pools * PER_GATHER
    assert int(tuning["max_worker_processes"]) == int(tuning["max_parallel_workers"]) + 8


def test_egernia_tier_8_is_the_parity_shape():
    """The bottom tier must reproduce the pre-registered parity run."""
    parity = yaml.safe_load((SUITE / "docker-compose.egernia-pins.yml").read_text())["services"]
    compose = yaml.safe_load((SUITE.parents[1] / "docker-compose.yml").read_text())["services"]
    tier8 = _services("egernia-8")
    for name, service in parity.items():
        assert tier8[name]["cpuset"] == service["cpuset"]
        assert tier8[name]["mem_limit"] == service["mem_limit"]
    assert tier8["db"]["command"] == compose["db"]["command"]
    assert tier8["tap-api"]["environment"]["TAP_API_WORKERS"] == "1"


@pytest.mark.parametrize("tier", TIERS)
def test_dachs_pins_follow_the_sizing_rules(tier):
    dachs = _services(f"dachs-{tier}")["dachs"]
    assert dachs["cpus"] == tier and dachs["cpuset"] == f"0-{tier - 1}"
    assert _bytes(dachs["mem_limit"]) == tier * GIB
    conf = dict(
        line.replace(" ", "").split("=", 1)
        for line in (SCALING / "pins" / f"dachs-postgres-{tier}.conf").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert _bytes(conf["shared_buffers"]) == tier * GIB // 4
    assert _bytes(conf["effective_cache_size"]) == tier * GIB * 3 // 4
    # the same parallel budget egernia's database gets at this tier
    egernia_db = dict(
        kv.split("=", 1) for kv in _services(f"egernia-{tier}")["db"]["command"][2::2]
    )
    for setting in ("max_parallel_workers", "max_worker_processes"):
        assert conf[setting] == egernia_db[setting]
    (mount,) = [v for v in dachs["volumes"] if f"dachs-postgres-{tier}.conf" in v]
    assert mount.endswith("/conf.d/90-tier.conf:ro")


def test_scaling_scenarios_keep_the_parity_workload():
    parity = yaml.safe_load((SUITE / "config" / "scenarios.yaml").read_text())
    scaling = yaml.safe_load((SCALING / "scenarios.yaml").read_text())
    for block in ("corpus", "mix", "guards"):
        assert scaling[block] == parity[block]
    grid = scaling["scenarios"]["scaling"]
    assert grid["per_class"] and grid["repetitions"] == 3
    assert grid["response_formats"] == parity["scenarios"]["compare"]["response_formats"]
    assert grid["maxrec"] == parity["scenarios"]["compare"]["maxrec"]
    assert (SCALING / "targets.yaml").read_text() == (SUITE / "config" / "targets.yaml").read_text()


def test_config_dir_selects_the_protocol():
    default_cfg, default_targets = cli._config(argparse.Namespace(config_dir=SUITE / "config"))
    scaling_cfg, scaling_targets = cli._config(argparse.Namespace(config_dir=SCALING))
    assert "compare" in default_cfg["scenarios"] and "scaling" not in default_cfg["scenarios"]
    assert "scaling" in scaling_cfg["scenarios"]
    assert set(default_targets) == set(scaling_targets) == {"egernia-local", "dachs-local"}


# -- one run directory, tiers measured one server at a time ------------------


def _fake_rung(*args, **kwargs):
    recorder = runner.Recorder()
    for i in range(3):
        recorder.add(
            runner.Sample(0.0, 0.0, "Q01", f"q{i}", 200, "", 0.01, 0.005, 100, -1, "", "sync", "")
        )
    return recorder, 6.0


def _fake_gates(run, chosen, entries, maxrec, prefix=""):
    outcome = {
        "targets": {
            name: {
                "vosi": {"tap_versions": ["1.1"], "maxrec_default": 10000},
                "taplint": {"passed": True, "errors_total": 0},
            }
            for name in chosen
        },
        "agreement": {"agreed": ["Q01"], "disagreed": []},
    }
    run.write_json(f"{prefix}gates.json", outcome)
    return outcome


@pytest.fixture
def protocol(tmp_path, monkeypatch):
    parity = yaml.safe_load((SUITE / "config" / "scenarios.yaml").read_text())
    config = tmp_path / "config"
    config.mkdir()
    (config / "scenarios.yaml").write_text(
        yaml.safe_dump(
            {
                "corpus": parity["corpus"],
                "mix": parity["mix"],
                "guards": parity["guards"],
                "scenarios": {
                    "tiny": {
                        "ladder": [2],
                        "warmup_seconds": 0,
                        "measure_seconds": 1,
                        "repetitions": 1,
                        "response_formats": ["csv"],
                        "maxrec": 100,
                        "generator_processes": 1,
                    }
                },
            }
        )
    )
    (config / "targets.yaml").write_text(
        textwrap.dedent("""\
        targets:
          - {name: a, server: egernia, base_url: http://a/tap}
          - {name: b, server: dachs, base_url: http://b/tap}
        """)
    )
    monkeypatch.setattr(runs, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(runner, "closed_loop_sharded", _fake_rung)
    monkeypatch.setattr(cli, "_run_gates", _fake_gates)
    return config


def test_tiers_measured_one_server_at_a_time_share_one_run(protocol, tmp_path):
    base = ["--config-dir", str(protocol), "compare", "--targets", "a", "b", "--scenario", "tiny"]
    assert cli.main([*base, "--tier", "8", "--gates-only"]) == 0
    (run_dir,) = (tmp_path / "results").iterdir()
    assert run_dir.name.endswith("-tap-compare-scaling")
    assert (run_dir / "t8-gates.json").exists() and not (run_dir / "summaries").exists()

    resume = ["--resume", run_dir.name]
    assert cli.main([*base, "--tier", "8", "--only", "a", *resume]) == 0
    assert cli.main([*base, "--tier", "16", "--only", "b", *resume]) == 0
    assert {p.stem for p in (run_dir / "summaries").iterdir()} == {
        "t8-a-csv-mix-c2-r1",
        "t16-b-csv-mix-c2-r1",
    }
    assert (run_dir / "t16-gates.json").exists()
    rows = json.loads((run_dir / "summary.json").read_text())
    assert {(r["target"], r["tier"]) for r in rows} == {("a", "8"), ("b", "16")}

    page = publish.render(run_dir, tmp_path / "docs").read_text()
    assert page.index("## Tier 8") < page.index("## Tier 16")
    assert page.count("### Gates") == 2 and "### csv" in page
    assert "measured one after the other" in page
    assert (tmp_path / "docs" / "summary.csv").read_text().splitlines()[0].endswith(",tier")


def test_only_must_name_a_compared_target(protocol):
    base = ["--config-dir", str(protocol), "compare"]
    with pytest.raises(SystemExit, match="--only"):
        cli.main([*base, "--targets", "a", "--only", "b", "--scenario", "tiny"])


def test_a_flat_run_renders_exactly_as_before(protocol, tmp_path):
    """Without --tier nothing changes: gates.json, plain keys, one section."""
    base = ["--config-dir", str(protocol), "compare", "--targets", "a", "b", "--scenario", "tiny"]
    assert cli.main(base) == 0
    (run_dir,) = (tmp_path / "results").iterdir()
    assert run_dir.name.endswith("-tap-compare") and (run_dir / "gates.json").exists()
    assert {p.stem for p in (run_dir / "summaries").iterdir()} == {
        "a-csv-mix-c2-r1",
        "b-csv-mix-c2-r1",
    }
    page = publish.render(run_dir, tmp_path / "docs").read_text()
    assert "## Gates" in page and "## csv" in page and "Tier" not in page
    assert "tier" not in (tmp_path / "docs" / "summary.csv").read_text().splitlines()[0]


# -- resource telemetry joined to rungs ---------------------------------------


def _resource_run(tmp_path, tier="8"):
    """One egernia rung (60 s window from t=1000) with cgroup samples: the db
    burns 2 cores, the api 1 core with 3 python processes, memory flat."""
    run_dir = tmp_path / "20260905T000000Z-abc12345-tap-compare-scaling"
    (run_dir / "samples").mkdir(parents=True)
    row = {
        "target": "egernia-local",
        "server": "egernia",
        "query_class": "mix",
        "response_format": "csv",
        "concurrency": 8,
        "repetition": 1,
        "requests": 6000,
        "error_fraction": 0.0,
        "rps": 100.0,
        "latency": {"p50_s": 0.01, "p95_s": 0.02},
        "ttfb": {"p95_s": 0.01},
        "mean_response_bytes": 100.0,
        "generator_cpu_peak": 0.1,
        "generator_guard_ok": True,
        "tier": tier,
    }
    samples = [
        runner.Sample(1000.0 + i * 10, 0.0, "Q01", "q", 200, "", 0.5, 0.1, 10, -1, "", "sync", "")
        for i in range(7)  # t_start 1000..1060, last ends at 1060.5
    ]
    runner.write_samples(
        samples, run_dir / "samples" / f"t{tier}-egernia-local-csv-mix-c8-r1.parquet"
    )
    lines = []
    for k in range(14):  # every 5 s from 998 to 1063
        t = 998 + 5 * k
        lines.append(
            json.dumps(
                {
                    "t": t,
                    "container": "egernia-db-1",
                    "cpu_usec": int(2e6 * t),
                    "mem_bytes": 2 * 2**30,
                    "python_procs": 0,
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "t": t,
                    "container": "egernia-tap-api-1",
                    "cpu_usec": int(1e6 * t),
                    "mem_bytes": 2**30,
                    "python_procs": 3,
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "t": t,
                    "container": "egernia-tap-executor-1",
                    "cpu_usec": 0,
                    "mem_bytes": 2**28,
                    "python_procs": 1,
                }
            )
        )
    (run_dir / "resources.jsonl").write_text("\n".join(lines) + "\n")
    return run_dir, row


def test_resources_join_cgroup_samples_to_the_rung_window(tmp_path):
    run_dir, row = _resource_run(tmp_path)
    (res,) = publish.resources(run_dir, [row]).values()
    assert res["coverage"] >= publish.MIN_COVERAGE
    assert res["cpu_cores"] == pytest.approx(3.0, rel=0.01)  # 2 + 1 + 0
    assert res["cpu_seconds_per_request"] == pytest.approx(3.0 * 60.5 / 6000, rel=0.01)
    assert res["mem_mean_bytes"] == 2 * 2**30 + 2**30 + 2**28
    assert res["mem_peak_bytes"] == res["mem_mean_bytes"]
    assert res["api_workers"] == 2  # three python processes = supervisor + 2 workers


def test_uncovered_rungs_get_no_resources(tmp_path):
    run_dir, row = _resource_run(tmp_path)
    text = (run_dir / "resources.jsonl").read_text().splitlines()
    (run_dir / "resources.jsonl").write_text("\n".join(text[-6:]) + "\n")  # last 10 s only
    assert publish.resources(run_dir, [row]) == {}


def test_report_carries_resource_tables_and_csv(tmp_path):
    run_dir, row = _resource_run(tmp_path)
    dachs = {**row, "target": "dachs-local", "server": "dachs", "rps": 10.0}
    (run_dir / "summary.json").write_text(json.dumps([row, dachs]))
    (run_dir / "environment.json").write_text(
        json.dumps({"git": {"sha": "abc12345"}, "seed": 1, "corpus_sha256": "c" * 64})
    )
    (run_dir / "t8-gates.json").write_text(json.dumps({"targets": {}}))
    page = publish.render(run_dir, tmp_path / "docs").read_text()
    assert "### resources, csv" in page and "### throughput vs CPU cores used (mix)" in page
    assert "| mix | 8 | — | — | — | — | 3.00 | 30.2 ms | 3.25 | 3.25 |" in page  # dachs uncovered
    assert "| csv | 8 | egernia-local | 2 | 100.0 | 3.00 | 33.3 | 30.2 |" in page
    csv_lines = (tmp_path / "docs" / "resources.csv").read_text().splitlines()
    assert csv_lines[0].startswith("target,tier,query_class") and len(csv_lines) == 2


def test_a_run_without_samples_has_no_resource_section(tmp_path):
    run_dir, row = _resource_run(tmp_path)
    (run_dir / "resources.jsonl").unlink()
    (run_dir / "summary.json").write_text(json.dumps([row]))
    (run_dir / "environment.json").write_text(
        json.dumps({"git": {"sha": "abc12345"}, "seed": 1, "corpus_sha256": "c" * 64})
    )
    (run_dir / "t8-gates.json").write_text(json.dumps({"targets": {}}))
    page = publish.render(run_dir, tmp_path / "docs").read_text()
    assert "resources" not in page.split("## Claims")[0].split("Tier 8")[1]
    assert not (tmp_path / "docs" / "resources.csv").exists()


# -- a run is tiered or flat, never both --------------------------------------


def test_resume_in_the_other_tier_mode_is_refused(protocol, tmp_path):
    base = ["--config-dir", str(protocol), "compare", "--targets", "a", "b", "--scenario", "tiny"]
    assert cli.main([*base, "--tier", "8", "--only", "a"]) == 0
    (run_dir,) = (tmp_path / "results").iterdir()
    env = json.loads((run_dir / "environment.json").read_text())
    assert env["tier_mode"] == "tiered" and env["tiers"] == ["8"]
    with pytest.raises(SystemExit, match=r"tiered run \(tiers 8\) and this invocation is flat"):
        cli.main([*base, "--resume", run_dir.name])
    assert cli.main([*base, "--tier", "16", "--only", "b", "--resume", run_dir.name]) == 0
    assert json.loads((run_dir / "environment.json").read_text())["tiers"] == ["8", "16"]

    assert cli.main(base) == 0  # a fresh flat run
    flat = next(p for p in (tmp_path / "results").iterdir() if p != run_dir)
    assert json.loads((flat / "environment.json").read_text())["tier_mode"] == "flat"
    with pytest.raises(SystemExit, match=r"flat run .* and this invocation is tiered"):
        cli.main([*base, "--tier", "8", "--resume", flat.name])


def test_a_run_predating_the_mode_record_is_classified_from_its_rungs(protocol, tmp_path):
    base = ["--config-dir", str(protocol), "compare", "--targets", "a", "b", "--scenario", "tiny"]
    assert cli.main([*base, "--tier", "8", "--only", "a"]) == 0
    (run_dir,) = (tmp_path / "results").iterdir()
    env_path = run_dir / "environment.json"
    env = json.loads(env_path.read_text())
    del env["tier_mode"], env["tiers"]
    env_path.write_text(json.dumps(env))
    assert cli._tier_mode_on_disk(run_dir) == "tiered"
    with pytest.raises(SystemExit, match="tiered run"):
        cli.main([*base, "--resume", run_dir.name])
    assert cli.main([*base, "--tier", "16", "--only", "b", "--resume", run_dir.name]) == 0
    env = json.loads(env_path.read_text())
    assert env["tier_mode"] == "tiered" and env["tiers"] == ["16"]  # recorded from here on


def test_server_memory_peak_is_the_peak_of_the_summed_series(tmp_path):
    """db peaks at 3 GiB while api is at 1, then api peaks at 3 while db is
    at 1: the server never held more than 4 GiB, whatever the per-container
    peaks add up to."""
    run_dir, row = _resource_run(tmp_path)
    lines = []
    for k in range(14):
        t = 998 + 5 * k
        db, api = (3, 1) if k % 2 else (1, 3)
        for container, mem in (("egernia-db-1", db), ("egernia-tap-api-1", api)):
            lines.append(
                json.dumps(
                    {
                        "t": t,
                        "container": container,
                        "cpu_usec": int(1e6 * t),
                        "mem_bytes": mem * 2**30,
                        "python_procs": 1,
                    }
                )
            )
        lines.append(
            json.dumps(
                {
                    "t": t,
                    "container": "egernia-tap-executor-1",
                    "cpu_usec": 0,
                    "mem_bytes": 0,
                    "python_procs": 1,
                }
            )
        )
    (run_dir / "resources.jsonl").write_text("\n".join(lines) + "\n")
    (res,) = publish.resources(run_dir, [row]).values()
    assert res["mem_peak_bytes"] == 4 * 2**30
    assert res["mem_mean_bytes"] == 4 * 2**30


def test_a_server_the_sampler_does_not_know_is_not_covered(tmp_path):
    run_dir, row = _resource_run(tmp_path)
    assert publish.resources(run_dir, [{**row, "server": "cadc-tap"}]) == {}
