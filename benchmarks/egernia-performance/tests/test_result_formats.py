"""Tests for the result-format family and the writer micro-benchmark.

Package 10 is a measurement, and a measurement is only worth the harness that
took it. Three things have to hold or the numbers mean something else: the
rows handed to every writer are identical, the per-format comparison
aggregates what it says it aggregates, and the memory ceilings the classifier
judges against come from the chart rather than from a constant that was true
once.
"""

import pathlib
import sys

SUITE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE))

from egernia_bench import serialize as serialize_mod  # noqa: E402
from egernia_bench.analyze import stats as stats_mod  # noqa: E402
from egernia_bench.orchestrate import runner  # noqa: E402

# ---------------------------------------------------------------------------
# The writer micro-benchmark
# ---------------------------------------------------------------------------


def test_rows_are_deterministic_and_wide():
    """Same seed, same rows — the corpus promise, applied to these rows too."""
    first = serialize_mod.rows(50, seed=20260823)
    second = serialize_mod.rows(50, seed=20260823)
    assert first == second
    assert serialize_mod.rows(50, seed=1) != first
    assert len(first[0]) == len(serialize_mod.Q11_COLUMNS)
    # Width is half of what this measures: a row of short strings would be a
    # different measurement wearing the same name.
    assert len(first[0][0]) > 40  # obs_publisher_did
    assert len(first[0][-1]) > 60  # access_url


def test_rows_types_match_the_declared_kinds():
    """The kinds are a promise about the Python types, so check the rows keep it."""
    expected = {"str": str, "int16": int, "float64": float}
    (row,) = serialize_mod.rows(1, seed=20260823)
    for value, (name, kind, _unit) in zip(row, serialize_mod.Q11_COLUMNS, strict=True):
        assert isinstance(value, expected[kind]), f"{name} is {type(value).__name__}, not {kind}"


def test_measure_formats_reports_every_format_once():
    measurements = serialize_mod.measure_formats([64], repetitions=2)
    assert [m["response_format"] for m in measurements] == list(serialize_mod.FORMATS)
    for m in measurements:
        assert m["rows"] == 64
        assert m["seconds_per_row"] > 0
        assert m["bytes_per_row"] > 0
        # The best can never exceed the median of the same samples.
        assert m["seconds_best"] <= m["seconds_median"] + 1e-12


def test_table_orders_cheapest_first():
    measurements = serialize_mod.measure_formats([64], repetitions=2)
    lines = [line for line in serialize_mod.table(measurements).splitlines() if line.strip()]
    body = [line.split()[0] for line in lines[2:]]
    costs = {m["response_format"]: m["seconds_per_row"] for m in measurements}
    assert body == sorted(body, key=lambda fmt: costs[fmt])


# ---------------------------------------------------------------------------
# The per-format comparison
# ---------------------------------------------------------------------------


def _measurement(fmt: str, p95: float, mean_bytes: float, requests: int = 100) -> dict:
    return {
        "kind": "format",
        "response_format": fmt,
        "by_class": {
            "Q11": {
                "requests": requests,
                "rps": 10.0,
                "mean_response_bytes": mean_bytes,
                "latency": {"p50_s": p95 / 2, "p95_s": p95},
            }
        },
    }


def test_format_comparison_groups_by_class_and_format():
    rows = runner.format_comparison(
        [
            _measurement("csv", 0.30, 2.4e6),
            _measurement("csv", 0.34, 2.4e6),
            _measurement("parquet", 0.10, 0.2e6),
            # Not a format measurement: must not be folded in.
            {"kind": "stress", "response_format": "csv", "by_class": {"Q11": {"requests": 5}}},
        ]
    )
    by_format = {r["response_format"]: r for r in rows}
    assert set(by_format) == {"csv", "parquet"}
    assert by_format["csv"]["repetitions"] == 2
    assert by_format["csv"]["requests"] == 200
    # The worst repetition's p95, not the mean of two percentiles — an average
    # of percentiles is not a percentile of anything.
    assert by_format["csv"]["latency_p95_s"] == 0.34
    assert by_format["parquet"]["mean_response_bytes"] == 0.2e6


def test_format_comparison_weights_bytes_by_requests():
    rows = runner.format_comparison(
        [_measurement("csv", 0.3, 100.0, requests=1), _measurement("csv", 0.3, 200.0, requests=3)]
    )
    assert rows[0]["mean_response_bytes"] == (100.0 + 3 * 200.0) / 4


def test_format_comparison_skips_measurements_with_no_requests():
    assert runner.format_comparison([_measurement("csv", 0.3, 0.0, requests=0)]) == []


def test_summarise_reports_the_mean_response_size():
    class Sample:
        def __init__(self, size):
            self.latency_s = 0.1
            self.ttfb_s = 0.01
            self.response_bytes = size
            self.status = 200
            self.error = ""

    summary = stats_mod.summarise([Sample(100), Sample(300)], 1.0)
    assert summary["mean_response_bytes"] == 200.0
    assert summary["response_bytes_total"] == 400.0


# ---------------------------------------------------------------------------
# Ceilings read from the chart
# ---------------------------------------------------------------------------


def test_memory_limits_come_from_the_chart_values():
    cfg = runner.load_config()
    resolved = runner.limits(cfg)
    values = cfg["chart_values"]
    assert resolved["postgres_memory_limit_bytes"] == runner._memory_bytes(
        values["postgresql"]["resources"]["limits"]["memory"], 0
    )
    assert resolved["tap_api_memory_limit_bytes"] == runner._memory_bytes(
        values["tapApi"]["resources"]["limits"]["memory"], 0
    )


def test_memory_quantities_parse_every_suffix_kubernetes_allows():
    assert runner._memory_bytes("12Gi", 0) == 12 << 30
    assert runner._memory_bytes("512Mi", 0) == 512 << 20
    assert runner._memory_bytes("1G", 0) == 10**9
    assert runner._memory_bytes("2048", 0) == 2048
    assert runner._memory_bytes(None, 7) == 7


def test_result_format_settings_name_only_supported_formats():
    """A typo in the config would otherwise surface as a 400 per request."""
    from egernia_core.query.votable import FORMATS

    settings = runner.load_config()["scenarios"]["result_formats"]
    for fmt in settings["formats"]:
        assert FORMATS[fmt][0] == fmt, f"{fmt} is not a canonical format key"
    assert settings["query_classes"]
    assert settings["repetitions"] >= 1


def test_each_format_is_handed_the_same_queries():
    """The whole family rests on this: the writers differ, the rows do not.

    `SingleClass` draws from a counter-based PRNG, so a workload shared across
    the formats hands the second one the sequence the first left off at. That
    would compare the writers on different queries — the one comparison this
    family exists not to make.
    """
    import pathlib as _pathlib

    from egernia_bench import corpus as corpus_mod
    from egernia_bench.load import runner as load_mod

    cfg = runner.load_config()
    entries = corpus_mod.build(cfg["scenarios"], cfg["datasets"], 5000)
    settings = cfg["scenarios"]["result_formats"]
    query_class = settings["query_classes"][-1]

    def sequence() -> list[str]:
        workload = load_mod.SingleClass(entries, query_class, seed=7000)
        return [workload.next().query_id for _ in range(40)]

    assert sequence() == sequence()
    # And the source says so: no workload outlives one measure() call.
    source = (
        _pathlib.Path(runner.__file__)
        .read_text()
        .split("def result_formats(")[1]
        .split("\ndef ")[0]
    )
    assert source.count("SingleClass(") == 1
    assert "workload=load_mod.SingleClass" in source


# ---------------------------------------------------------------------------
# The two format plots (shared grouped-bar builder)
# ---------------------------------------------------------------------------

_COMPARISON = [
    {
        "query_class": "Q01",
        "response_format": "csv",
        "latency_p95_s": 0.11,
        "mean_response_bytes": 2_500_000,
    },
    {
        "query_class": "Q01",
        "response_format": "votable",
        "latency_p95_s": 0.25,
        "mean_response_bytes": 5_000_000,
    },
    # Q02 was never measured against votable: the pair is absent, not zero
    {
        "query_class": "Q02",
        "response_format": "csv",
        "latency_p95_s": 0.90,
        "mean_response_bytes": 20_000_000,
    },
]


def _bar_heights(plotter_method, tmp_path, monkeypatch):
    import matplotlib

    matplotlib.use("Agg")
    from egernia_bench.analyze import report as report_mod

    (tmp_path / "plots").mkdir(parents=True, exist_ok=True)
    plotter = report_mod.Plotter(tmp_path, {"format_comparison": _COMPARISON})
    heights = []
    original = report_mod.Plotter._axes  # a staticmethod: no self

    def recording_axes(xlabel, ylabel, title):
        fig, ax = original(xlabel, ylabel, title)
        real_bar = ax.bar

        def capture(positions, values, **kwargs):
            heights.append(list(values))
            return real_bar(positions, values, **kwargs)

        ax.bar = capture
        return fig, ax

    monkeypatch.setattr(report_mod.Plotter, "_axes", staticmethod(recording_axes))
    getattr(plotter, plotter_method)()
    return heights


def test_an_unmeasured_format_pair_is_a_gap_not_a_zero_bar(tmp_path, monkeypatch):
    """Q02 was never run against votable. Drawing that as a zero bar would
    claim the writer produced a zero-byte response in no time; it has to be
    NaN so the bar is simply absent."""
    import math

    heights = _bar_heights("format_bytes", tmp_path, monkeypatch)
    # one series per query class, one bar per format (csv, votable)
    assert len(heights) == 2
    assert heights[0] == [2_500_000 / 2**20, 5_000_000 / 2**20]
    assert heights[1][0] == 20_000_000 / 2**20
    assert math.isnan(heights[1][1])


def test_latency_bars_are_milliseconds(tmp_path, monkeypatch):
    heights = _bar_heights("format_latency", tmp_path, monkeypatch)
    assert heights[0] == [110.0, 250.0]
