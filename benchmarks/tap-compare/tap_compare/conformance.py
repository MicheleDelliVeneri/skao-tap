"""The conformance gate: STILTS taplint, before any timed rung.

A performance comparison between a conformant server and a non-conformant
one is a comparison between different amounts of work. taplint's report is
archived verbatim in the run directory; ERROR lines in the stages this
benchmark exercises fail the gate.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import urllib.request

STILTS_URL = "https://www.star.bris.ac.uk/~mbt/stilts/stilts.jar"
CACHE = pathlib.Path.home() / ".cache" / "tap-compare"
JAVA_IMAGE = "eclipse-temurin:21-jre"

#: taplint stages this benchmark's workload actually exercises. ERRORs in
#: other stages (e.g. upload, examples) are reported but do not block.
BLOCKING_STAGES = ("CAP", "TMV", "TMS", "TMC", "QGE", "UWS")

LINE = re.compile(r"^([EWIS])-([A-Z]+)-", re.MULTILINE)


def stilts_jar() -> pathlib.Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    jar = CACHE / "stilts.jar"
    if not jar.exists():
        urllib.request.urlretrieve(STILTS_URL, jar)
    return jar


def run_taplint(tap_url: str, out_path: pathlib.Path, timeout_s: float = 900.0) -> dict:
    """taplint in a Java container on the host network; report archived.

    Returns {"errors_total", "errors_blocking", "by_stage", "report_path"}.
    """
    jar = stilts_jar()
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=host",
            "-v",
            f"{jar}:/stilts.jar:ro",
            JAVA_IMAGE,
            "java",
            "-jar",
            "/stilts.jar",
            "taplint",
            f"tapurl={tap_url}",
            "report=EWIS",
            "maxtable=5",
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    report = result.stdout + ("\n--- stderr ---\n" + result.stderr if result.stderr else "")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    return parse_report(report) | {"report_path": str(out_path)}


def parse_report(report: str) -> dict:
    """Count taplint messages by severity and stage."""
    by_stage: dict[str, dict[str, int]] = {}
    for severity, stage in LINE.findall(report):
        counts = by_stage.setdefault(stage, {})
        counts[severity] = counts.get(severity, 0) + 1
    errors_total = sum(c.get("E", 0) for c in by_stage.values())
    errors_blocking = sum(by_stage.get(s, {}).get("E", 0) for s in BLOCKING_STAGES)
    return {
        "errors_total": errors_total,
        "errors_blocking": errors_blocking,
        "by_stage": by_stage,
        "passed": errors_blocking == 0,
    }
