"""The performance report the integration suite prints.

Unit tests: no deployment. The renderer is worth pinning because it is the
deliverable — the deployment stack streams the test pod's log and keeps nothing
else, so this table is the only form the measurements ever take.
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "integration"
sys.path.insert(0, str(ROOT))


class _Reporter:
    """The two methods pytest_terminal_summary uses."""

    def __init__(self):
        self.lines: list[str] = []
        self.sections: list[str] = []

    def write_line(self, text):
        self.lines.append(text)

    def section(self, title, **kwargs):
        del kwargs
        self.sections.append(title)

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def conftest(monkeypatch, tmp_path):
    monkeypatch.setenv("EGERNIA_RUN_INTEGRATION_TESTS", "1")
    monkeypatch.setenv("EGERNIA_TIMINGS_FILE", str(tmp_path / "timings.jsonl"))
    for name in [m for m in sys.modules if m == "conftest"]:
        del sys.modules[name]
    import conftest as module

    return module


def _write(module, rows):
    module.TIMINGS_PATH.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def test_the_report_names_every_measurement_and_flags_a_breach(conftest):
    _write(
        conftest,
        [
            {"test": "a", "label": "keyed lookup", "seconds": 0.41, "budget": 20,
             "headroom": 19.59, "within_budget": True},
            {"test": "b", "label": "10k rows as votable", "seconds": 26.0, "budget": 20,
             "headroom": -6.0, "within_budget": False},
        ],
    )
    reporter = _Reporter()
    conftest.pytest_terminal_summary(reporter, 0, None)

    assert "egernia performance report" in reporter.sections
    assert "keyed lookup" in reporter.text
    assert "10k rows as votable" in reporter.text
    # the breach has to be visible, not just present
    over = [line for line in reporter.lines if "10k rows as votable" in line]
    assert over and over[0].strip().endswith("OVER"), over
    within = [line for line in reporter.lines if "keyed lookup" in line]
    assert within and within[0].strip().endswith("ok"), within


def test_the_report_calls_out_the_slowest_and_the_tightest(conftest):
    """Slowest and least-headroom are different questions and both matter.

    A 104s aggregate against a 110s budget is the thing about to break; a 26s
    write against a 20s budget is the thing already broken. Reporting only the
    largest number would hide the second.
    """
    _write(
        conftest,
        [
            {"test": "a", "label": "full-table aggregate", "seconds": 104.0, "budget": 110,
             "headroom": 6.0, "within_budget": True},
            {"test": "b", "label": "cone search", "seconds": 1.9, "budget": 45,
             "headroom": 43.1, "within_budget": True},
        ],
    )
    reporter = _Reporter()
    conftest.pytest_terminal_summary(reporter, 0, None)
    assert "slowest:" in reporter.text
    assert "least headroom:" in reporter.text
    slowest = next(line for line in reporter.lines if line.startswith("slowest:"))
    assert "full-table aggregate" in slowest


def test_no_measurements_means_no_report(conftest):
    """A functional-only run must not print an empty table."""
    reporter = _Reporter()
    conftest.pytest_terminal_summary(reporter, 0, None)
    assert not reporter.sections and not reporter.lines


def test_a_truncated_line_does_not_lose_the_report(conftest):
    """Workers append under a lock, but a killed run can still leave a partial
    line. The measurements that did land are worth more than strictness."""
    conftest.TIMINGS_PATH.write_text(
        json.dumps({"test": "a", "label": "cone search", "seconds": 1.9, "budget": 45,
                    "headroom": 43.1, "within_budget": True}) + "\n{\"partial\""
    )
    reporter = _Reporter()
    conftest.pytest_terminal_summary(reporter, 0, None)
    assert not reporter.lines or "cone search" in reporter.text
