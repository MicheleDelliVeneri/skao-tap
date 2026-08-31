"""The comparison publisher: aggregation, the tie rule, and the page."""

import json

from tap_compare import publish


def _row(target, rps, p95, cls="Q05", fmt="votable", conc=8, rep=1):
    return {
        "target": target,
        "server": target.split("-")[0],
        "query_class": cls,
        "response_format": fmt,
        "concurrency": conc,
        "repetition": rep,
        "requests": 100,
        "error_fraction": 0.0,
        "rps": rps,
        "latency": {"p50_s": p95 / 2, "p95_s": p95},
        "ttfb": {"p95_s": p95 / 3},
        "mean_response_bytes": 1000.0,
        "generator_cpu_peak": 0.1,
        "generator_guard_ok": True,
    }


def _rows(a_rps, b_rps):
    rows = []
    for rep, (a, b) in enumerate(zip(a_rps, b_rps, strict=True), start=1):
        rows.append(_row("egernia-local", a, 0.1, rep=rep))
        rows.append(_row("dachs-local", b, 0.1, rep=rep))
    return rows


def _keys():
    return ("Q05", "votable", 8, "egernia-local"), ("Q05", "votable", 8, "dachs-local")


def test_clearly_separated_intervals_name_a_winner():
    cells = publish.aggregate(_rows([100, 102, 98], [50, 51, 49]))
    assert publish.verdict(cells, *_keys()) == "egernia-local"


def test_overlapping_intervals_are_a_tie():
    """Two throughputs differing by less than their spread are the same
    throughput, whatever their means say."""
    cells = publish.aggregate(_rows([100, 80, 120], [95, 115, 75]))
    assert publish.verdict(cells, *_keys()) == "tie"


def test_small_differences_are_a_tie_even_with_tight_intervals():
    """The practical-significance floor: under 10% apart is a tie."""
    cells = publish.aggregate(_rows([105.0, 105.1, 104.9], [100.0, 100.1, 99.9]))
    assert publish.verdict(cells, *_keys()) == "tie"


def test_single_repetition_never_claims_a_difference():
    cells = publish.aggregate(_rows([100], [50]))
    assert publish.verdict(cells, *_keys()) == "tie"


def test_render_writes_the_full_report(tmp_path):
    run_dir = tmp_path / "20260901T000000Z-abc12345-tap-compare"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(json.dumps(_rows([100, 102, 98], [50, 51, 49])))
    (run_dir / "environment.json").write_text(
        json.dumps({"git": {"sha": "abc12345deadbeef"}, "seed": 424242, "corpus_sha256": "c" * 64})
    )
    (run_dir / "gates.json").write_text(
        json.dumps(
            {
                "targets": {
                    "egernia-local": {
                        "vosi": {"tap_versions": ["1.1"], "maxrec_default": 10000},
                        "taplint": {"passed": True, "errors_total": 0},
                    },
                    "dachs-local": {
                        "vosi": {"tap_versions": ["1.1"], "maxrec_default": 20000},
                        "taplint": {"passed": True, "errors_total": 0},
                    },
                },
                "agreement": {"agreed": ["Q05"], "disagreed": []},
            }
        )
    )
    out = tmp_path / "docs"
    page = publish.render(run_dir, out)
    text = page.read_text()
    assert "## Gates" in text and "PASS (0 errors)" in text
    assert "egernia-local" in text and "dachs-local" in text
    assert "| Q05 | 8 |" in text and "egernia-local |" in text  # winner column
    assert "## Claims" in text and "## Threats to validity" in text
    assert (out / "summary.csv").read_text().startswith("target,server,query_class")
