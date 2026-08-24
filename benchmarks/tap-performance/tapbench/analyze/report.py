"""Plots and the HTML report.

Two rules shape this file.

A plot that cannot be drawn says so. Every figure is registered with what it
needs, and when the data is not there the report shows the plot's name and the
reason it is missing rather than quietly leaving a gap — a report with fifteen
of sixteen plots and no explanation is indistinguishable from one where the
sixteenth was never asked for.

Nothing is recomputed here that was measured elsewhere. The report reads
summary.json and the Parquet files; it does not re-derive a percentile, so the
number in a figure is the number in the CSV.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib

import matplotlib

matplotlib.use("Agg")  # no display on a benchmark host, and none wanted
import matplotlib.pyplot as plt
import numpy as np

log = logging.getLogger("tapbench.report")

PALETTE = {
    "primary": "#1f4e79",
    "secondary": "#c1440e",
    "third": "#2e7d32",
    "fourth": "#6a1b9a",
    "grey": "#757575",
    "warn": "#b71c1c",
}
FIGSIZE = (8.0, 4.5)


@dataclasses.dataclass
class Figure:
    name: str
    title: str
    caption: str
    path: pathlib.Path | None = None
    missing_reason: str = ""


class Plotter:
    def __init__(self, run_dir: pathlib.Path, summary: dict) -> None:
        self.run_dir = run_dir
        self.summary = summary
        self.plots_dir = run_dir / "plots"
        self.plots_dir.mkdir(parents=True, exist_ok=True)
        self.figures: list[Figure] = []
        self.runs = summary.get("runs", [])

    # -- helpers ------------------------------------------------------------

    def _save(self, fig, name: str, title: str, caption: str) -> Figure:
        png = self.plots_dir / f"{name}.png"
        fig.tight_layout()
        fig.savefig(png, dpi=140)
        # SVG as well as PNG: the PNG goes in the report, the SVG is what
        # survives being pasted into a document at a different size.
        fig.savefig(self.plots_dir / f"{name}.svg")
        plt.close(fig)
        entry = Figure(name, title, caption, png)
        self.figures.append(entry)
        return entry

    def _skip(self, name: str, title: str, reason: str) -> Figure:
        entry = Figure(name, title, "", None, reason)
        self.figures.append(entry)
        log.info("plot %s skipped: %s", name, reason)
        return entry

    def _select(self, **criteria) -> list[dict]:
        out = []
        for run in self.runs:
            if all(run.get(k) == v for k, v in criteria.items()):
                out.append(run)
        return out

    @staticmethod
    def _axes(xlabel: str, ylabel: str, title: str):
        fig, ax = plt.subplots(figsize=FIGSIZE)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(alpha=0.3, linestyle=":")
        return fig, ax

    # -- concurrency --------------------------------------------------------

    def rps_vs_concurrency(self) -> Figure:
        runs = [r for r in self._select(kind="concurrency") if r.get("http")]
        if not runs:
            return self._skip(
                "rps_vs_concurrency",
                "Throughput vs concurrency",
                "no concurrency-sweep measurements in this run",
            )
        fig, ax = self._axes(
            "concurrent clients", "requests/second", "Throughput vs offered concurrency"
        )
        for dataset in sorted({r["dataset"] for r in runs}):
            points = self._grouped(runs, dataset, "rps")
            if not points:
                continue
            xs = [p[0] for p in points]
            means = [p[1] for p in points]
            errs = [p[2] for p in points]
            ax.errorbar(xs, means, yerr=errs, marker="o", capsize=3, label=f"{dataset}")
        ax.set_xscale("log", base=2)
        ax.legend(title="dataset")
        return self._save(
            fig,
            "rps_vs_concurrency",
            "Throughput vs concurrency",
            "Error bars are 95% Student-t intervals across repetitions. A curve "
            "that flattens has found a ceiling; where it flattens is the "
            "sustainable capacity, and the bottleneck table says which resource "
            "it hit.",
        )

    def latency_vs_concurrency(self) -> Figure:
        runs = [r for r in self._select(kind="concurrency") if r.get("http")]
        if not runs:
            return self._skip(
                "latency_vs_concurrency",
                "Latency vs concurrency",
                "no concurrency-sweep measurements in this run",
            )
        fig, ax = self._axes(
            "concurrent clients", "latency (s)", "Latency percentiles vs offered concurrency"
        )
        for percentile, colour in (
            ("p50_s", PALETTE["primary"]),
            ("p95_s", PALETTE["secondary"]),
            ("p99_s", PALETTE["warn"]),
        ):
            for dataset in sorted({r["dataset"] for r in runs}):
                points = self._grouped(runs, dataset, f"latency.{percentile}")
                if not points:
                    continue
                ax.errorbar(
                    [p[0] for p in points],
                    [p[1] for p in points],
                    yerr=[p[2] for p in points],
                    marker="o",
                    capsize=3,
                    color=colour,
                    alpha=0.9,
                    label=f"{percentile.replace('_s', '')} {dataset}",
                )
        ax.set_xscale("log", base=2)
        ax.set_yscale("log")
        ax.legend(fontsize="small", ncols=2)
        return self._save(
            fig,
            "latency_vs_concurrency",
            "Latency vs concurrency",
            "Log-log, because both axes span orders of magnitude. Until the "
            "service saturates, p50 is flat and only the tail moves; past "
            "saturation every percentile rises together, which is queueing.",
        )

    def errors_vs_concurrency(self) -> Figure:
        runs = [r for r in self._select(kind="concurrency") if r.get("http")]
        if not runs:
            return self._skip(
                "errors_vs_concurrency",
                "Errors vs concurrency",
                "no concurrency-sweep measurements in this run",
            )
        fig, ax = self._axes(
            "concurrent clients", "failed requests (%)", "Error rate vs offered concurrency"
        )
        for dataset in sorted({r["dataset"] for r in runs}):
            points = self._grouped(runs, dataset, "error_fraction")
            if points:
                ax.plot(
                    [p[0] for p in points], [100 * p[1] for p in points], marker="o", label=dataset
                )
        ax.set_xscale("log", base=2)
        ax.axhline(
            1.0, color=PALETTE["warn"], linestyle="--", alpha=0.6, label="1% saturation signal"
        )
        ax.legend()
        return self._save(
            fig,
            "errors_vs_concurrency",
            "Errors vs concurrency",
            "The 1% line is one of the suite's saturation signals. Errors "
            "appearing before throughput flattens usually means a refusal path "
            "(pool timeout, 503) rather than a hard ceiling.",
        )

    def _grouped(
        self, runs: list[dict], dataset: str, field: str
    ) -> list[tuple[float, float, float]]:
        """(x, mean, half-interval) per concurrency level for one dataset."""
        from . import stats as stats_mod

        buckets: dict[float, list[float]] = {}
        for run in runs:
            if run["dataset"] != dataset:
                continue
            value = run["http"]
            for part in field.split("."):
                value = (value or {}).get(part) if isinstance(value, dict) else None
            if value is None:
                continue
            buckets.setdefault(run["concurrency"], []).append(value)
        points = []
        for x in sorted(buckets):
            ci = stats_mod.mean_ci(buckets[x])
            half = (ci["ci95_high"] - ci["mean"]) if ci.get("ci95_high") is not None else 0.0
            points.append((x, ci["mean"], half))
        return points

    # -- replicas -----------------------------------------------------------

    def rps_vs_replicas(self) -> Figure:
        runs = [r for r in self._select(kind="fixed_replicas") if r.get("http")]
        if not runs:
            return self._skip(
                "rps_vs_replicas",
                "Throughput vs replicas",
                "no fixed-replica measurements in this run",
            )
        fig, ax = self._axes(
            "API replicas", "successful requests/second", "Throughput vs replica count"
        )
        buckets: dict[int, list[float]] = {}
        for run in runs:
            buckets.setdefault(run["replicas"], []).append(run["http"].get("successful_rps") or 0.0)
        xs = sorted(buckets)
        ax.plot(
            xs,
            [max(buckets[x]) for x in xs],
            marker="o",
            color=PALETTE["primary"],
            label="measured",
        )
        if xs:
            one = max(buckets[xs[0]])
            ax.plot(
                xs,
                [one * x / xs[0] for x in xs],
                linestyle="--",
                color=PALETTE["grey"],
                label="linear from 1 replica",
            )
        ax.legend()
        # The gap is only contention where each point is a ceiling. Where a
        # replica count served everything it was offered, the gap is the
        # ladder's, and saying otherwise attributes the shortfall to PostgreSQL
        # on the strength of a rate nobody raised.
        ceilings = {
            c["replicas"] for c in self.summary.get("replica_capacity") or [] if c["bracketed"]
        }
        open_ended = sorted(set(xs) - ceilings)
        note = (
            "The dashed line is what perfect scaling would look like. The gap "
            "between them is the shared resource — here, one PostgreSQL."
            if not open_ended
            else "The dashed line is what perfect scaling would look like. At "
            + ", ".join(str(x) for x in open_ended)
            + " replicas the service met every request it was offered, so the "
            "point is the rate offered rather than a ceiling and the gap to the "
            "line is the ladder's, not the service's."
        )
        return self._save(fig, "rps_vs_replicas", "Throughput vs replicas", note)

    def scaling_efficiency(self) -> Figure:
        # Only ceilings, because efficiency is a ratio of ceilings. A curve
        # drawn through rates the service met in full measures the ladder that
        # was offered: this run would have drawn 1.0, 1.0, 0.5, 0.25 from four
        # replica counts that all served every request they were given.
        if not self._select(kind="fixed_replicas"):
            return self._skip(
                "scaling_efficiency",
                "Scaling efficiency",
                "no fixed-replica measurements in this run",
            )
        capacities = [c for c in (self.summary.get("replica_capacity") or []) if c["bracketed"]]
        buckets = {c["replicas"]: c["rps"] for c in capacities}
        if not buckets:
            return self._skip(
                "scaling_efficiency",
                "Scaling efficiency",
                "no replica count was pushed past what it could serve, so no "
                "throughput here is a ceiling to take a ratio of",
            )
        if 1 not in buckets:
            return self._skip(
                "scaling_efficiency",
                "Scaling efficiency",
                "no single-replica ceiling to normalise against",
            )
        base = buckets[1]
        xs = sorted(buckets)
        fig, ax = self._axes("API replicas", "efficiency", "Scaling efficiency")
        ax.plot(
            xs,
            [buckets[x] / (x * base) if base else 0.0 for x in xs],
            marker="o",
            color=PALETTE["third"],
        )
        ax.axhline(1.0, color=PALETTE["grey"], linestyle="--", alpha=0.6)
        ax.set_ylim(0, 1.2)
        return self._save(
            fig,
            "scaling_efficiency",
            "Scaling efficiency",
            "throughput(N) / (N x throughput(1)). 1.0 is linear scaling; the "
            "shortfall is what the replicas are contending over.",
        )

    # -- dataset size -------------------------------------------------------

    def _size_axis(self) -> dict[str, float]:
        datasets = self.summary.get("datasets", {})
        return {name: (info.get("database_bytes") or 0) / 2**30 for name, info in datasets.items()}

    def rps_vs_size(self) -> Figure:
        sizes = self._size_axis()
        runs = [r for r in self.runs if r.get("http") and r["dataset"] in sizes]
        if len(sizes) < 2 or not runs:
            return self._skip(
                "rps_vs_size",
                "Throughput vs database size",
                "fewer than two datasets measured in this run",
            )
        fig, ax = self._axes(
            "database size (GiB)", "requests/second", "Throughput vs database size"
        )
        for concurrency in sorted({r["concurrency"] for r in runs if r.get("concurrency")}):
            points = []
            for run in runs:
                if run.get("concurrency") != concurrency:
                    continue
                points.append((sizes[run["dataset"]], run["http"].get("rps") or 0.0))
            if len(points) > 1:
                points.sort()
                ax.plot(
                    [p[0] for p in points],
                    [p[1] for p in points],
                    marker="o",
                    label=f"c={concurrency}",
                )
        ax.legend(fontsize="small")
        return self._save(
            fig,
            "rps_vs_size",
            "Throughput vs database size",
            "Where this falls away is where the working set stops fitting in "
            "memory. Compare with the cache-hit-ratio plot: the two turn over "
            "together, or the fall is not I/O.",
        )

    def latency_vs_size(self) -> Figure:
        sizes = self._size_axis()
        runs = [r for r in self.runs if r.get("http") and r["dataset"] in sizes]
        if len(sizes) < 2 or not runs:
            return self._skip(
                "latency_vs_size",
                "Latency vs database size",
                "fewer than two datasets measured in this run",
            )
        fig, ax = self._axes("database size (GiB)", "latency (s)", "Latency vs database size")
        for field, label in (("p50_s", "p50"), ("p95_s", "p95"), ("p99_s", "p99")):
            points = []
            for run in runs:
                value = (run["http"].get("latency") or {}).get(field)
                if value is not None:
                    points.append((sizes[run["dataset"]], value))
            if len(points) > 1:
                points.sort()
                ax.plot([p[0] for p in points], [p[1] for p in points], marker="o", label=label)
        ax.set_yscale("log")
        ax.legend()
        return self._save(
            fig,
            "latency_vs_size",
            "Latency vs database size",
            "Index lookups should be almost flat against size; anything that "
            "rises steeply is doing work proportional to the table.",
        )

    def cache_hit_vs_size(self) -> Figure:
        sizes = self._size_axis()
        points = []
        for run in self.runs:
            ratio = (run.get("postgres") or {}).get("cache_hit_ratio")
            if ratio is not None and run["dataset"] in sizes:
                points.append((sizes[run["dataset"]], 100 * ratio))
        if len(points) < 2:
            return self._skip(
                "cache_hit_vs_size",
                "Cache hit ratio vs size",
                "no PostgreSQL deltas across two or more datasets",
            )
        points.sort()
        fig, ax = self._axes(
            "database size (GiB)", "buffer cache hit ratio (%)", "Cache hit ratio vs database size"
        )
        ax.plot(
            [p[0] for p in points], [p[1] for p in points], marker="o", color=PALETTE["secondary"]
        )
        ax.axhline(99.0, color=PALETTE["grey"], linestyle="--", alpha=0.6, label="99%")
        ax.legend()
        return self._save(
            fig,
            "cache_hit_vs_size",
            "Cache hit ratio vs database size",
            "shared_buffers is 1.5 GiB here. The point where this leaves 99% is "
            "the point where the benchmark starts measuring the disk.",
        )

    # -- resource vs throughput --------------------------------------------

    def _resource_vs_throughput(
        self, metric: str, name: str, title: str, ylabel: str, caption: str, scale: float = 1.0
    ) -> Figure:
        points = []
        for run in self.runs:
            resources = run.get("resources") or {}
            value = resources.get(metric)
            rps = (run.get("http") or {}).get("rps")
            if value is not None and rps:
                points.append((rps, value * scale))
        if len(points) < 3:
            return self._skip(name, title, f"fewer than three measurements carry {metric}")
        points.sort()
        fig, ax = self._axes("requests/second", ylabel, title)
        ax.scatter(
            [p[0] for p in points], [p[1] for p in points], color=PALETTE["primary"], alpha=0.8
        )
        return self._save(fig, name, title, caption)

    def tap_cpu_vs_throughput(self) -> Figure:
        return self._resource_vs_throughput(
            "tap_api_cpu_cores_mean",
            "tap_cpu_vs_throughput",
            "API CPU vs throughput",
            "API CPU (cores)",
            "The slope is CPU cost per request. A curve that bends upwards means "
            "each request is getting more expensive as load rises — contention, "
            "not capacity.",
        )

    def postgres_cpu_vs_throughput(self) -> Figure:
        return self._resource_vs_throughput(
            "postgres_cpu_cores_mean",
            "postgres_cpu_vs_throughput",
            "PostgreSQL CPU vs throughput",
            "PostgreSQL CPU (cores)",
            "Compare the plateau here with the API's: whichever flattens first "
            "at its limit is the binding constraint.",
        )

    def postgres_io_vs_throughput(self) -> Figure:
        return self._resource_vs_throughput(
            "postgres_fs_read_bytes_mean",
            "postgres_io_vs_throughput",
            "PostgreSQL read I/O vs throughput",
            "read bytes/second",
            "Read bandwidth against offered throughput. On a dataset that fits "
            "in memory this stays near zero however hard the service is pushed.",
        )

    # -- per query class ----------------------------------------------------

    def query_class_rps(self) -> Figure:
        classes = self.summary.get("by_query_class") or {}
        if not classes:
            return self._skip(
                "query_class_rps", "Throughput by query class", "no per-class aggregation available"
            )
        names = sorted(classes)
        values = [classes[c].get("rps") or 0.0 for c in names]
        fig, ax = self._axes("query class", "requests/second", "Throughput by query class")
        ax.bar(names, values, color=PALETTE["primary"])
        ax.tick_params(axis="x", rotation=45)
        return self._save(
            fig,
            "query_class_rps",
            "Throughput by query class",
            "Shares of the same run, so these are not independent capacities: a "
            "class's rate reflects both its cost and its weight in the mix.",
        )

    def query_class_latency(self) -> Figure:
        classes = self.summary.get("by_query_class") or {}
        if not classes:
            return self._skip(
                "query_class_latency",
                "Latency by query class",
                "no per-class aggregation available",
            )
        names = sorted(classes)
        p95 = [(classes[c].get("latency") or {}).get("p95_s") or 0.0 for c in names]
        p99 = [(classes[c].get("latency") or {}).get("p99_s") or 0.0 for c in names]
        x = np.arange(len(names))
        fig, ax = self._axes("query class", "latency (s)", "p95 and p99 by query class")
        ax.bar(x - 0.2, p95, width=0.4, label="p95", color=PALETTE["primary"])
        ax.bar(x + 0.2, p99, width=0.4, label="p99", color=PALETTE["secondary"])
        ax.set_xticks(x, names, rotation=45)
        ax.set_yscale("log")
        ax.legend()
        return self._save(
            fig,
            "query_class_latency",
            "Latency by query class",
            "The spread between classes is the argument for measuring them "
            "separately: a single p95 over a mixed workload is a weighted "
            "average of distributions with different shapes.",
        )

    def class_size_heatmap(self) -> Figure:
        sizes = self._size_axis()
        classes: dict[str, dict[str, float]] = {}
        for run in self.runs:
            for cls, summary in (run.get("by_class") or {}).items():
                value = (summary.get("latency") or {}).get("p95_s")
                if value is not None:
                    classes.setdefault(cls, {})[run["dataset"]] = value
        datasets = [
            d
            for d in sorted(sizes, key=lambda d: sizes[d])
            if any(d in row for row in classes.values())
        ]
        if len(datasets) < 2 or not classes:
            return self._skip(
                "class_size_heatmap",
                "Query class x database size",
                "needs per-class latency on at least two datasets",
            )
        names = sorted(classes)
        grid = np.full((len(names), len(datasets)), np.nan)
        for i, cls in enumerate(names):
            for j, dataset in enumerate(datasets):
                if dataset in classes[cls]:
                    grid[i, j] = classes[cls][dataset]
        fig, ax = plt.subplots(figsize=(1.4 * len(datasets) + 3.5, 0.4 * len(names) + 2))
        image = ax.imshow(np.log10(grid), aspect="auto", cmap="viridis")
        ax.set_xticks(range(len(datasets)), [f"{d}\n{sizes[d]:.0f} GiB" for d in datasets])
        ax.set_yticks(range(len(names)), names)
        ax.set_title("p95 latency by query class and database size")
        for i in range(len(names)):
            for j in range(len(datasets)):
                if not np.isnan(grid[i, j]):
                    ax.text(
                        j,
                        i,
                        f"{grid[i, j] * 1000:.0f}",
                        ha="center",
                        va="center",
                        color="white",
                        fontsize=7,
                    )
        fig.colorbar(image, ax=ax, label="log10(p95 seconds)")
        return self._save(
            fig,
            "class_size_heatmap",
            "Query class x database size",
            "Cell labels are p95 in milliseconds. Read across a row: a row that "
            "grows with size is doing work proportional to the data.",
        )

    # -- samples-derived ----------------------------------------------------

    def result_size_vs_latency(self) -> Figure:
        table = self._load_samples()
        if table is None:
            return self._skip(
                "result_size_vs_latency", "Result size vs latency", "no request samples on disk"
            )
        sizes = np.asarray(table["response_bytes"])
        latencies = np.asarray(table["latency_s"])
        keep = (sizes > 0) & (latencies > 0)
        if keep.sum() < 50:
            return self._skip(
                "result_size_vs_latency",
                "Result size vs latency",
                "too few successful samples with a body",
            )
        fig, ax = self._axes("response bytes", "latency (s)", "Latency vs response size")
        ax.scatter(sizes[keep], latencies[keep], s=3, alpha=0.15, color=PALETTE["primary"])
        ax.set_xscale("log")
        ax.set_yscale("log")
        return self._save(
            fig,
            "result_size_vs_latency",
            "Result size vs latency",
            "Each point is one request. A floor that rises with size is the "
            "serialisation cost; vertical scatter at fixed size is queueing.",
        )

    def run_to_run_variability(self) -> Figure:
        from . import stats as stats_mod

        buckets: dict[str, list[float]] = {}
        for run in self.runs:
            rps = (run.get("http") or {}).get("rps")
            if rps is None:
                continue
            key = f"{run.get('dataset')}/c{run.get('concurrency')}/r{run.get('replicas')}"
            buckets.setdefault(key, []).append(rps)
        repeated = {k: v for k, v in buckets.items() if len(v) > 1}
        if not repeated:
            return self._skip(
                "run_to_run_variability",
                "Run-to-run variability",
                "no configuration was measured more than once",
            )
        keys = sorted(repeated)
        fig, ax = self._axes(
            "configuration", "coefficient of variation (%)", "Run-to-run variability of throughput"
        )
        values = []
        for key in keys:
            ci = stats_mod.mean_ci(repeated[key])
            values.append(100 * (ci["stddev"] or 0.0) / ci["mean"] if ci["mean"] else 0.0)
        ax.bar(keys, values, color=PALETTE["fourth"])
        ax.tick_params(axis="x", rotation=60, labelsize="x-small")
        ax.axhline(5.0, color=PALETTE["warn"], linestyle="--", alpha=0.6, label="5%")
        ax.legend()
        return self._save(
            fig,
            "run_to_run_variability",
            "Run-to-run variability",
            "Standard deviation over mean, per configuration. This is the "
            "resolution of the whole exercise: a difference smaller than this "
            "is not a difference.",
        )

    def _load_samples(self):
        import pyarrow.parquet as pq

        files = sorted((self.run_dir / "samples").glob("*.parquet"))
        if not files:
            return None
        tables = [pq.read_table(f) for f in files]
        import pyarrow as pa

        return pa.concat_tables(tables).to_pydict()

    # -- KEDA dashboards ----------------------------------------------------

    def keda_dashboard(self, scenario: dict) -> Figure:
        """One synchronised dashboard per autoscaling scenario."""
        import pyarrow.parquet as pq

        # The measurement key, not the scenario id: the series are written as
        # `keda-K3.parquet`, and looking for `K3.parquet` skipped every
        # autoscaling dashboard in the family — the run's whole point — with a
        # reason that was not true.
        key = scenario.get("key") or f"keda-{scenario['id']}"
        parquet = self.run_dir / "metrics" / f"{key}.parquet"
        if not parquet.exists():
            return self._skip(
                f"keda_{scenario['id']}",
                f"{scenario['id']} — {scenario.get('description', '')}",
                f"no metrics parquet at metrics/{key}.parquet",
            )
        rows = pq.read_table(parquet).to_pydict()
        metrics: dict[str, list[tuple[float, float]]] = {}
        for metric, t, value in zip(rows["metric"], rows["t"], rows["value"], strict=True):
            metrics.setdefault(metric, []).append((t, value))
        for series in metrics.values():
            series.sort()

        # Keyed the same way as the series, and for the same reason: named by
        # the scenario id, this found nothing, and the three panels that draw
        # request data — offered against served, latency, error rate — came out
        # blank on every dashboard in the family while still being captioned.
        samples_file = self.run_dir / "samples" / f"{key}.parquet"
        samples = None
        if samples_file.exists():
            samples = pq.read_table(samples_file).to_pydict()

        t0 = scenario.get("t_start") or (
            min(t for series in metrics.values() for t, _ in series) if metrics else 0
        )

        def rel(series):
            return [t - t0 for t, _ in series], [v for _, v in series]

        def legend_or_say_why(ax, missing: str) -> None:
            """Label a panel that has data; label the absence when it has none.

            An empty axis under a heading reads as "measured, and flat", and a
            path mistake has produced exactly that twice in this file. The
            missing data is drawn on the panel instead — and matplotlib's
            "no artists with labels" warning stops being the only place it was
            reported.
            """
            if ax.get_legend_handles_labels()[0]:
                ax.legend(fontsize="x-small", ncols=3)
            else:
                ax.text(
                    0.5,
                    0.5,
                    missing,
                    transform=ax.transAxes,
                    ha="center",
                    va="center",
                    fontsize="small",
                    color=PALETTE["warn"],
                )

        no_samples = f"no request samples at samples/{key}.parquet"

        panels = 6
        fig, axes = plt.subplots(panels, 1, figsize=(11, 2.1 * panels), sharex=True)

        # 1: offered vs served
        ax = axes[0]
        if samples:
            finished = np.asarray(samples["t_start"]) + np.asarray(samples["latency_s"])
            ok = (np.asarray(samples["status"]) >= 200) & (np.asarray(samples["status"]) < 300)
            edges = np.arange(finished.min(), finished.max() + 5, 5.0)
            served, _ = np.histogram(finished, bins=edges)
            good, _ = np.histogram(finished[ok], bins=edges)
            centres = (edges[:-1] + edges[1:]) / 2 - t0
            ax.plot(centres, served / 5.0, label="completed/s", color=PALETTE["grey"])
            ax.plot(centres, good / 5.0, label="successful/s", color=PALETTE["third"])
            offered = np.asarray(samples["t_offered"])
            offered = offered[offered > 0]
            if offered.size:
                arrivals, _ = np.histogram(offered, bins=edges)
                ax.plot(
                    centres,
                    arrivals / 5.0,
                    label="offered/s",
                    linestyle="--",
                    color=PALETTE["primary"],
                )
        ax.set_ylabel("requests/s")
        legend_or_say_why(ax, no_samples)
        ax.grid(alpha=0.3, linestyle=":")

        # 2: latency
        ax = axes[1]
        if samples:
            from types import SimpleNamespace

            reconstructed = [
                SimpleNamespace(t_start=t, latency_s=lat, status=st, error=err)
                for t, lat, st, err in zip(
                    samples["t_start"],
                    samples["latency_s"],
                    samples["status"],
                    samples["error"],
                    strict=True,
                )
            ]
            for percentile, colour in ((95, PALETTE["secondary"]), (99, PALETTE["warn"])):
                windows = rolling_percentile_local(reconstructed, percentile)
                if windows:
                    ax.plot(
                        [w[0] - t0 for w in windows],
                        [w[1] for w in windows],
                        label=f"p{percentile}",
                        color=colour,
                    )
            slo = scenario.get("slo_p95_s")
            if slo:
                ax.axhline(
                    slo, color=PALETTE["grey"], linestyle="--", alpha=0.7, label=f"SLO {slo}s"
                )
        ax.set_ylabel("latency (s)")
        ax.set_yscale("log")
        legend_or_say_why(ax, no_samples)
        ax.grid(alpha=0.3, linestyle=":")

        # 3: error rate
        ax = axes[2]
        if samples:
            finished = np.asarray(samples["t_start"]) + np.asarray(samples["latency_s"])
            bad = ~((np.asarray(samples["status"]) >= 200) & (np.asarray(samples["status"]) < 300))
            edges = np.arange(finished.min(), finished.max() + 5, 5.0)
            total, _ = np.histogram(finished, bins=edges)
            failed, _ = np.histogram(finished[bad], bins=edges)
            with np.errstate(divide="ignore", invalid="ignore"):
                fraction = np.where(total > 0, 100 * failed / total, 0.0)
            ax.plot((edges[:-1] + edges[1:]) / 2 - t0, fraction, color=PALETTE["warn"])
        else:
            legend_or_say_why(ax, no_samples)
        ax.set_ylabel("errors (%)")
        ax.grid(alpha=0.3, linestyle=":")

        # 4: the scaler's own metric and its threshold
        ax = axes[3]
        for metric, colour, label in (
            ("keda_scaler_metrics_value", PALETTE["primary"], "KEDA metric"),
            ("tap_oldest_queued_job_seconds", PALETTE["third"], "queue backlog (s)"),
        ):
            if metrics.get(metric):
                xs, ys = rel(metrics[metric])
                ax.plot(xs, ys, label=label, color=colour)
        threshold = scenario.get("threshold")
        if threshold:
            ax.axhline(
                threshold, color=PALETTE["warn"], linestyle="--", label=f"threshold {threshold}"
            )
        ax.set_ylabel("scaler metric")
        ax.legend(fontsize="x-small", ncols=3)
        ax.grid(alpha=0.3, linestyle=":")

        # 5: replicas
        ax = axes[4]
        for metric, colour, label in (
            ("executor_replicas_desired", PALETTE["primary"], "desired"),
            ("executor_replicas_ready", PALETTE["third"], "ready"),
            ("api_replicas_desired", PALETTE["fourth"], "api desired"),
            ("api_replicas_ready", PALETTE["secondary"], "api ready"),
        ):
            if metrics.get(metric):
                xs, ys = rel(metrics[metric])
                ax.step(xs, ys, where="post", label=label, color=colour)
        ax.set_ylabel("replicas")
        ax.legend(fontsize="x-small", ncols=4)
        ax.grid(alpha=0.3, linestyle=":")

        # 6: resources
        ax = axes[5]
        for metric, colour, label in (
            ("tap_api_cpu_cores", PALETTE["primary"], "API CPU (cores)"),
            ("tap_executor_cpu_cores", PALETTE["fourth"], "executor CPU (cores)"),
            ("postgres_cpu_cores", PALETTE["secondary"], "PostgreSQL CPU (cores)"),
        ):
            if metrics.get(metric):
                xs, ys = rel(metrics[metric])
                ax.plot(xs, ys, label=label, color=colour)
        if metrics.get("postgres_fs_read_bytes"):
            twin = ax.twinx()
            xs, ys = rel(metrics["postgres_fs_read_bytes"])
            twin.plot(
                xs, [y / 2**20 for y in ys], color=PALETTE["grey"], alpha=0.7, label="PG read MiB/s"
            )
            twin.set_ylabel("MiB/s")
            twin.legend(fontsize="x-small", loc="upper right")
        ax.set_ylabel("CPU cores")
        ax.set_xlabel("seconds from scenario start")
        ax.legend(fontsize="x-small", ncols=3)
        ax.grid(alpha=0.3, linestyle=":")

        # The stage stamps, marked on every panel so the timings can be read
        # against what the service was doing at that instant.
        stamps = (scenario.get("timings") or {}).get("stamps") or {}
        for name in ("T1", "T2", "T6", "T7", "T8"):
            when = stamps.get(name)
            if when:
                for panel in axes:
                    panel.axvline(when - t0, color="black", alpha=0.25, linewidth=1)
                axes[0].annotate(
                    name,
                    (when - t0, axes[0].get_ylim()[1]),
                    fontsize="x-small",
                    ha="center",
                    va="top",
                )

        fig.suptitle(f"{scenario['id']} — {scenario.get('description', '')}")
        return self._save(
            fig,
            f"keda_{scenario['id']}",
            f"{scenario['id']} — {scenario.get('description', '')}",
            "One time base for every panel, so a latency spike can be read "
            "against the replica count and the scaler metric at that instant. "
            "Vertical lines are the stage stamps.",
        )

    def draw_all(self) -> list[Figure]:
        for method in (
            self.rps_vs_concurrency,
            self.latency_vs_concurrency,
            self.errors_vs_concurrency,
            self.rps_vs_replicas,
            self.scaling_efficiency,
            self.rps_vs_size,
            self.latency_vs_size,
            self.tap_cpu_vs_throughput,
            self.postgres_cpu_vs_throughput,
            self.postgres_io_vs_throughput,
            self.cache_hit_vs_size,
            self.query_class_rps,
            self.query_class_latency,
            self.class_size_heatmap,
            self.result_size_vs_latency,
            self.run_to_run_variability,
        ):
            try:
                method()
            except Exception as exc:  # a broken plot must not lose the run
                log.exception("plot %s failed", method.__name__)
                self._skip(
                    method.__name__, method.__name__, f"plotting raised {type(exc).__name__}: {exc}"
                )
        for scenario in self.summary.get("keda", []) or []:
            try:
                self.keda_dashboard(scenario)
            except Exception as exc:
                log.exception("keda dashboard %s failed", scenario.get("id"))
                self._skip(
                    f"keda_{scenario.get('id')}",
                    str(scenario.get("id")),
                    f"plotting raised {type(exc).__name__}: {exc}",
                )
        return self.figures


def rolling_percentile_local(samples, percentile: float, window_s: float = 10.0):
    from .keda import rolling_percentile

    return rolling_percentile(samples, percentile, window_s)
