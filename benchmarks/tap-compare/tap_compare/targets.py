"""Target descriptors: which TAP services this harness may point at.

A target is data, not code: everything server-specific the runner needs —
where the TAP root is, which classes it can answer, what may be claimed of
the results — lives here, so the measurement path stays identical across
servers.
"""

from __future__ import annotations

import dataclasses
import pathlib

import yaml

CONFIG_DIR = pathlib.Path(__file__).resolve().parents[1] / "config"


@dataclasses.dataclass(frozen=True)
class Target:
    name: str  # e.g. "egernia-local"
    server: str  # e.g. "egernia", "dachs", "cadc-tap"
    base_url: str  # the TAP root: {base_url}/sync, {base_url}/async
    #: srcnet.* classes only exist on egernia; every other server gets the
    #: portable ObsCore subset regardless of what a scenario asks for
    portable_only: bool = True
    notes: str = ""

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def load(path: pathlib.Path | None = None) -> dict[str, Target]:
    raw = yaml.safe_load((path or CONFIG_DIR / "targets.yaml").read_text())
    targets = {}
    for entry in raw["targets"]:
        target = Target(**entry)
        targets[target.name] = target
    return targets
