"""Run directories, provenance and resumability.

Three rules, and each exists because of a way benchmark results go wrong:

* **Never overwrite.** A run directory is named for its start time and refused
  if it exists. A benchmark whose results can be silently replaced cannot be
  compared with itself.
* **Resumable.** Every unit of work writes a marker when it completes, and a
  resumed run skips what is already there. A 40-hour matrix that has to start
  again from zero after a laptop sleeps is a matrix that never finishes.
* **Provenance with the numbers, not beside them.** Commit, image ids,
  versions, resource caps, seed and corpus hash live in the result directory.
  A throughput figure without them is a rumour.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import platform
import shutil
import subprocess
import time
import typing

log = logging.getLogger("egernia_bench.runs")

SUITE = pathlib.Path(__file__).resolve().parents[1]
RESULTS = SUITE / "results"


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(SUITE),
            text=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout.strip()
    except Exception:  # a tarball without .git is still benchmarkable
        return ""


def git_sha() -> str:
    return _git("rev-parse", "HEAD")


def git_dirty() -> bool:
    return bool(_git("status", "--porcelain"))


@dataclasses.dataclass
class Run:
    """One invocation of one scenario family, on disk."""

    path: pathlib.Path
    scenario: str

    @property
    def samples_dir(self) -> pathlib.Path:
        return self._dir("samples")

    @property
    def metrics_dir(self) -> pathlib.Path:
        return self._dir("metrics")

    @property
    def postgres_dir(self) -> pathlib.Path:
        return self._dir("postgres")

    @property
    def kube_dir(self) -> pathlib.Path:
        return self._dir("kubernetes")

    @property
    def explain_dir(self) -> pathlib.Path:
        return self._dir("explain")

    def _dir(self, name: str) -> pathlib.Path:
        path = self.path / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- resumability -------------------------------------------------------

    def done(self, key: str) -> bool:
        return (self.path / "state" / f"{key}.done").exists()

    def mark_done(self, key: str, detail: dict | None = None) -> None:
        state = self.path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / f"{key}.done").write_text(
            json.dumps({"finished_at": time.time(), **(detail or {})}, indent=2)
        )

    def write_json(self, name: str, payload: typing.Any) -> pathlib.Path:
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # allow_nan=False: a NaN in a summary is a metric that was never
        # measured wearing the clothes of one that was.
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str, allow_nan=False))
        return path

    # -- validity -----------------------------------------------------------

    def invalidate(self, reason: str, detail: dict | None = None) -> None:
        """Mark the run questionable without deleting anything.

        Marked rather than discarded on purpose: the samples from an invalid
        run are still the best evidence of what went wrong, and a suite that
        quietly drops them teaches you nothing.
        """
        path = self.path / "invalid.json"
        existing = json.loads(path.read_text()) if path.exists() else {"reasons": []}
        existing["reasons"].append({"reason": reason, "at": time.time(), **(detail or {})})
        path.write_text(json.dumps(existing, indent=2, default=str))
        log.error("run marked invalid: %s", reason)


def new_run(scenario: str, resume: str | None = None) -> Run:
    """A fresh run directory, or an existing one to continue."""
    if resume:
        path = RESULTS / resume
        if not path.is_dir():
            raise SystemExit(f"cannot resume {resume}: {path} does not exist")
        log.info("resuming %s", path.name)
        return Run(path=path, scenario=scenario)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    sha = git_sha()[:8] or "nogit"
    path = RESULTS / f"{stamp}-{sha}-{scenario}"
    if path.exists():
        # Same second, same commit, same scenario: rather than pick a suffix
        # and hope, refuse. Overwriting a result set is the one thing this
        # suite must never do.
        raise SystemExit(f"{path} already exists; refusing to overwrite results")
    path.mkdir(parents=True)
    log.info("results -> %s", path)
    return Run(path=path, scenario=scenario)


def resolve(argument: str | None) -> pathlib.Path | None:
    """A run directory from whatever the caller had to hand.

    Accepts an absolute path, a path relative to the working directory, a bare
    run name, or nothing at all (meaning the newest). The Makefile runs these
    commands from the suite directory while a person reading the results is
    usually somewhere else, so insisting on one form only would be a papercut
    on every invocation.
    """
    if not argument:
        return latest_run()
    candidate = pathlib.Path(argument)
    if candidate.is_dir():
        return candidate
    for base in (RESULTS, pathlib.Path.cwd()):
        alternative = base / pathlib.Path(argument).name
        if alternative.is_dir():
            return alternative
    return None


def latest_run() -> pathlib.Path | None:
    candidates = sorted(p for p in RESULTS.glob("*") if p.is_dir())
    return candidates[-1] if candidates else None


# -- artefact readers ---------------------------------------------------------
# The rebuild half of the suite's "analysis is re-derivable from artefacts"
# rule, shared by the runner and the report so the shapes cannot drift.


def read_metrics_rows(path: pathlib.Path) -> list[dict]:
    """A metrics Parquet back as the row dicts Prometheus.collect produced."""
    import pyarrow.parquet as pq

    table = pq.read_table(path).to_pydict()
    return [
        {"metric": m, "labels": lab, "t": t, "value": v}
        for m, lab, t, v in zip(
            table["metric"], table["labels"], table["t"], table["value"], strict=True
        )
    ]


def read_samples(path: pathlib.Path) -> list:
    """A samples Parquet back as objects with the fields the analysis reads."""
    import types

    import pyarrow.parquet as pq

    table = pq.read_table(path, columns=["t_start", "latency_s", "status", "error"]).to_pydict()
    return [
        types.SimpleNamespace(t_start=t, latency_s=lat, status=st, error=err)
        for t, lat, st, err in zip(
            table["t_start"], table["latency_s"], table["status"], table["error"], strict=True
        )
    ]


def environment(
    cluster_versions: dict,
    image_digests: dict,
    hardware: dict,
    seed: int,
    corpus_hash: str,
    chart_values: str,
) -> dict:
    """Everything needed to know whether two results are comparable."""
    free = shutil.disk_usage(str(SUITE)).free
    return {
        "recorded_at": time.time(),
        "git": {
            "sha": git_sha(),
            "dirty": git_dirty(),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "describe": _git("describe", "--always", "--dirty"),
        },
        "images": image_digests,
        # Deliberately no hostname or username: this file is published to
        # GitHub Pages under docs/performance/, and neither is needed to judge
        # whether two runs are comparable.
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "free_disk_bytes": free,
        },
        "cluster": cluster_versions,
        "hardware_budget": hardware,
        "seed": seed,
        "corpus_sha256": corpus_hash,
        "chart_values_sha256": hashlib.sha256(chart_values.encode()).hexdigest(),
        "chart_values": chart_values,
    }
