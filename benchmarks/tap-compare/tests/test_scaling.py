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
