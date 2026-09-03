"""The shipped PostgreSQL settings follow the sizing rule in
docs/postgres-performance.md ("Sizing the server to its container").

Pinned because the image's defaults (128MB shared_buffers, 8 parallel
workers cluster-wide) cost tap-compare's aggregation class a third of its
throughput and doubled its tail latency, and nothing else would notice
them quietly coming back.
"""

import pathlib

import pytest
import yaml
from egernia_core.config import settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
PER_GATHER = 2  # PostgreSQL's max_parallel_workers_per_gather default, left alone
SERVER_OWN_WORKERS = 8  # launchers, I/O workers, autovacuum

_UNITS = {
    "kB": 2**10,
    "MB": 2**20,
    "GB": 2**30,
    "k": 2**10,
    "m": 2**20,
    "g": 2**30,
    "Mi": 2**20,
    "Gi": 2**30,
}


def _bytes(value: str) -> int:
    digits = value.rstrip("".join(_UNITS))
    return int(digits) * _UNITS[value[len(digits) :]]


def _check(tuning: dict, memory_limit: int, pool_slots: int) -> None:
    assert _bytes(tuning["shared_buffers"]) == memory_limit // 4
    workers = int(tuning["max_parallel_workers"])
    assert workers >= pool_slots * PER_GATHER
    assert int(tuning["max_worker_processes"]) >= workers + SERVER_OWN_WORKERS


@pytest.fixture(scope="module")
def compose_tuning():
    command = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["db"]["command"]
    assert command[0] == "postgres"
    flags = command[1:]
    assert flags[::2] == ["-c"] * (len(flags) // 2)
    return dict(kv.split("=", 1) for kv in flags[1::2])


def test_compose_settings_fit_the_comparison_pins(compose_tuning):
    pins = yaml.safe_load(
        (ROOT / "benchmarks/tap-compare/docker-compose.egernia-pins.yml").read_text()
    )
    memory = _bytes(pins["services"]["db"]["mem_limit"])
    # one API process and one executor, each with the default pool
    _check(compose_tuning, memory, pool_slots=2 * settings.db_pool_max)


def test_chart_settings_fit_the_in_chart_pod():
    values = yaml.safe_load((ROOT / "charts/egernia/values.yaml").read_text())
    pg = values["postgresql"]
    memory = _bytes(pg["resources"]["limits"]["memory"])
    api = values["tapApi"]["replicas"] * values["tapApi"]["workers"]
    slots = (api + values["tapExecutor"]["replicas"]) * values["config"]["dbPoolMax"]
    _check(pg["tuning"], memory, pool_slots=slots)
    assert _bytes(pg["tuning"]["effective_cache_size"]) == memory * 3 // 4
