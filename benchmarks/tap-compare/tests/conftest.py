import pathlib
import sys

import pytest
import yaml

SUITE = pathlib.Path(__file__).resolve().parents[1]
REPO = SUITE.parents[1]
sys.path.insert(0, str(SUITE))


@pytest.fixture(scope="module")
def cfg():
    return {
        "scenarios": yaml.safe_load((SUITE / "config" / "scenarios.yaml").read_text()),
        "datasets": yaml.safe_load((REPO / "dataset" / "config" / "datasets.yaml").read_text()),
    }
