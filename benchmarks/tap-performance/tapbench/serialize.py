"""What one row costs each result writer, with nothing else in the way.

The cluster families measure a *request*: parse, plan, execute, fetch, write,
respond. That is the number a client sees, and it is the wrong instrument for
asking which writer is expensive, because the writer is a tenth of it and the
database is most of the rest. This runs the writers on their own, in process,
against rows built here — no cluster, no database, no HTTP — so a per-row cost
is a per-row cost.

It is a measurement like any other in this suite, which means it is
deterministic (the rows come from the corpus PRNG, seeded), it records what it
ran on, and it reports the spread rather than one number: the minimum over
repetitions is the honest figure for a CPU cost, and the median beside it says
whether the machine was quiet.
"""

from __future__ import annotations

import json
import logging
import pathlib
import platform
import statistics
import sys
import time
import typing

from tapcore.query.results import ColumnMeta, RowLimiter, stream

from . import corpus as corpus_mod

log = logging.getLogger("tapbench.serialize")

# The Q11 projection, typed as the cursor description types it. This is the
# shape the finding is about: eleven columns, five of them doubles, five of
# them wide text, one small integer.
Q11_COLUMNS: tuple[tuple[str, str, str | None], ...] = (
    ("obs_publisher_did", "str", None),
    ("obs_id", "str", None),
    ("obs_collection", "str", None),
    ("dataproduct_type", "str", None),
    ("calib_level", "int16", None),
    ("s_ra", "float64", "deg"),
    ("s_dec", "float64", "deg"),
    ("s_fov", "float64", "deg"),
    ("t_min", "float64", "d"),
    ("t_max", "float64", "d"),
    ("access_url", "str", None),
)

COLLECTIONS = ("SKA-Mid-Continuum", "SKA-Low-Continuum", "SKA-Mid-Spectral", "MeerKAT-Legacy")
DATAPRODUCT_TYPES = ("image", "cube", "visibility", "spectrum")

FORMATS = ("votable", "csv", "tsv", "json", "parquet", "arrow")


def columns() -> list[ColumnMeta]:
    return [
        ColumnMeta(name, kind=kind, unit=unit, ucd="pos.eq.ra" if name == "s_ra" else None)
        for name, kind, unit in Q11_COLUMNS
    ]


def rows(count: int, seed: int) -> list[tuple]:
    """`count` ObsCore rows, from the generator's own hash.

    Widths matter as much as types: the identifier, the publisher DID and the
    access URL are what make an ObsCore row wide, and a row of short strings
    would measure a different thing and call it the same name.
    """
    out: list[tuple] = []
    for index in range(1, count + 1):
        ra = 360.0 * corpus_mod.rnd(str(seed), index, "ra")
        dec = corpus_mod.object_position(str(seed), index)[1]
        fov = 0.02 + corpus_mod.rnd(str(seed), index, "fov") * 4.0
        t_min = 58000.0 + corpus_mod.rnd(str(seed), index, "tmin") * 2000.0
        exptime = 10.0 + corpus_mod.rnd(str(seed), index, "exp") * 28790.0
        product = 1 + index % 3
        out.append(
            (
                f"ivo://skao.int/srcnet?ska:obs:{index:012d}/product-{product}",
                f"ska:obs:{index:012d}",
                COLLECTIONS[index % len(COLLECTIONS)],
                DATAPRODUCT_TYPES[(index * 8 + product) % len(DATAPRODUCT_TYPES)],
                index % 4,
                ra,
                dec,
                fov,
                t_min,
                t_min + exptime / 86400.0,
                "https://data.srcnet.skao.int/artifact/"
                f"ska:obs:{index:012d}/product-{product}/data.fits",
            )
        )
    return out


def _time_one(cols: list[ColumnMeta], data: list[tuple], fmt: str) -> tuple[float, int]:
    total = 0
    started = time.perf_counter()
    for chunk in stream(cols, RowLimiter(data, len(data)), fmt):
        total += len(chunk)
    return time.perf_counter() - started, total


def measure_formats(
    row_counts: typing.Sequence[int],
    formats: typing.Sequence[str] = FORMATS,
    *,
    repetitions: int = 15,
    seed: int = 20260823,
) -> list[dict]:
    """Per (rows, format): seconds per row, bytes per row, bytes per second."""
    cols = columns()
    out: list[dict] = []
    for count in row_counts:
        data = rows(count, seed)
        for fmt in formats:
            _time_one(cols, data, fmt)  # discarded: imports pyarrow, warms code
            timings, size = [], 0
            for _ in range(repetitions):
                elapsed, size = _time_one(cols, data, fmt)
                timings.append(elapsed)
            best = min(timings)
            out.append(
                {
                    "rows": count,
                    "response_format": fmt,
                    "repetitions": repetitions,
                    # The minimum is the CPU cost; anything above it is
                    # something else on the machine, which is why the median
                    # is reported next to it rather than instead of it.
                    "seconds_best": best,
                    "seconds_median": statistics.median(timings),
                    "seconds_per_row": best / count,
                    "bytes": size,
                    "bytes_per_row": size / count,
                    "bytes_per_second": size / best,
                    "rows_per_second": count / best,
                }
            )
            log.info(
                "%6d rows %-8s %7.2f us/row  %6.0f B/row  %6.1f MB/s",
                count,
                fmt,
                1e6 * best / count,
                size / count,
                size / best / 1e6,
            )
    return out


def report(
    row_counts: typing.Sequence[int] = (1000, 10000),
    formats: typing.Sequence[str] = FORMATS,
    *,
    repetitions: int = 15,
    seed: int = 20260823,
) -> dict:
    measurements = measure_formats(row_counts, formats, repetitions=repetitions, seed=seed)
    return {
        "generated_at": time.time(),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "columns": [{"name": name, "kind": kind, "unit": unit} for name, kind, unit in Q11_COLUMNS],
        "seed": seed,
        "measurements": measurements,
    }


def table(measurements: list[dict]) -> str:
    """The measurements as a fixed-width table, per row count."""
    lines: list[str] = []
    for count in sorted({m["rows"] for m in measurements}):
        subset = [m for m in measurements if m["rows"] == count]
        cheapest = min(m["seconds_per_row"] for m in subset)
        lines.append(f"\n{count} rows, {len(Q11_COLUMNS)} columns (the Q11 projection)")
        lines.append(
            f"  {'format':9s} {'us/row':>9s} {'ms total':>9s} "
            f"{'B/row':>8s} {'MB/s':>8s} {'vs best':>8s}"
        )
        for m in sorted(subset, key=lambda m: m["seconds_per_row"]):
            lines.append(
                f"  {m['response_format']:9s} {1e6 * m['seconds_per_row']:9.2f} "
                f"{1e3 * m['seconds_best']:9.1f} {m['bytes_per_row']:8.0f} "
                f"{m['bytes_per_second'] / 1e6:8.1f} "
                f"{m['seconds_per_row'] / cheapest:7.1f}x"
            )
    return "\n".join(lines)


def write(payload: dict, path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path
