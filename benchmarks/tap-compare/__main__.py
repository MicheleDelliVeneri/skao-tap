"""Entry point: `python benchmarks/tap-compare <command>` from the repo root."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from tap_compare.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
