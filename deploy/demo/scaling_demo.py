"""egernia: a hundred gigabytes, answered from another machine.

A marimo notebook. Reactive by design, so the sliders below re-run only what
depends on them — turn concurrency up and the latency plot redraws while the
Prometheus panel keeps streaming.

Run it with:

    make demo-notebook HOST=tap.example.org

Nothing here is generated or cached: every number is measured against the
deployment named by EGERNIA_BASE_URL at the moment the cell runs.
"""

import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def _():
    import os
    import time

    import altair as alt
    import httpx
    import marimo as mo
    import pandas as pd

    BASE = os.environ.get("EGERNIA_BASE_URL", "http://localhost:8080").rstrip("/")
    TAP = f"{BASE}/tap"
    API = f"{BASE}/api/v1"
    PROM = f"{BASE}/prometheus"
    return API, BASE, PROM, TAP, alt, httpx, mo, os, pd, time


@app.cell
def _(BASE, TAP, httpx, mo):
    # One reachability check, stated plainly: everything below is meaningless
    # if this fails, and "connection refused" three cells later reads as a
    # broken demo rather than a DNS entry nobody made.
    try:
        _probe = httpx.get(f"{TAP}/availability", timeout=10)
        _reachable = _probe.status_code == 200
        _detail = f"HTTP {_probe.status_code}"
    except Exception as exc:
        _reachable, _detail = False, f"{type(exc).__name__}: {exc}"

    mo.md(
        f"""
        # egernia — a hundred gigabytes, from another machine

        Service: `{BASE}`
        {"**reachable**" if _reachable else f"**NOT reachable** — {_detail}"}

        {
            ""
            if _reachable
            else "This machine cannot see the service. Check that the host resolves to "
            "the ingress address (`make demo-status` prints both)."
        }
        """
    )
    return


@app.cell
def _(API, TAP, httpx, mo, pd):
    # What is actually deployed, read from the service rather than assumed.
    _tables = httpx.get(f"{API}/tables", timeout=30).json()["tables"]
    _by_schema = {}
    for _t in _tables:
        _by_schema.setdefault(_t["schema"], []).append(_t["name"])

    _caps = httpx.get(f"{TAP}/capabilities", timeout=15).text
    _models = [line.strip() for line in _caps.splitlines() if "dataModel" in line]

    mo.vstack(
        [
            mo.md("## What this deployment publishes"),
            mo.ui.table(
                pd.DataFrame(
                    [
                        {"schema": s, "tables": len(n), "names": ", ".join(sorted(n)[:6])}
                        for s, n in sorted(_by_schema.items())
                    ]
                ),
                selection=None,
            ),
            mo.md(
                "Both metadata domains are live: `srcnet` is the observatory data "
                "product model, `software` the software discovery model, and "
                "`ivoa` the ObsCore view generated over the first. Every one of "
                "them is queryable in the same ADQL."
                + ("\n\nDeclared data models: " + "; ".join(_models) if _models else "")
            ),
        ]
    )
    return


@app.cell
def _(mo):
    mo.md(
        """
        ## 1. A large table, a selective query, a fast answer

        The claim is not "everything is fast". It is that a query which lets
        the database use an index is fast *regardless of how much data it is
        not looking at* — which is the whole reason the footprint column is a
        GiST-indexed pgsphere geometry rather than the STC-S text beside it.
        """
    )
    return


@app.cell
def _(mo):
    radius = mo.ui.slider(
        0.05, 5.0, value=0.5, step=0.05, label="cone radius (degrees)", show_value=True
    )
    radius
    return (radius,)


@app.cell
def _(TAP, httpx, mo, radius, time):
    _adql = f"""
    SELECT TOP 1000 obs_publisher_did, s_ra, s_dec, em_min, em_max, access_url
    FROM ivoa.obscore
    WHERE 1 = INTERSECTS(s_region_geom,
                         CIRCLE('ICRS', 150.0, -30.0, {radius.value}))
    """
    _t0 = time.perf_counter()
    _r = httpx.post(
        f"{TAP}/sync",
        data={"LANG": "ADQL", "QUERY": _adql, "RESPONSEFORMAT": "csv"},
        timeout=120,
    )
    _elapsed = time.perf_counter() - _t0
    _rows = max(len(_r.text.strip().splitlines()) - 1, 0)

    mo.vstack(
        [
            mo.md(f"```sql{_adql}```"),
            mo.hstack(
                [
                    mo.stat(f"{1000 * _elapsed:.0f} ms", label="wall clock", bordered=True),
                    mo.stat(f"{_rows:,}", label="rows returned", bordered=True),
                    mo.stat(f"{len(_r.content) / 1024:.0f} KiB", label="response", bordered=True),
                ]
            ),
            mo.md(
                "The time barely moves with the radius until the *result* grows: "
                "the index finds the candidates, and what is left is the cost of "
                "writing rows out."
                if _elapsed < 1.0
                else "Slower than a second here usually means a cold cache — the first "
                "query after a deploy reads from disk. Run it again."
            ),
        ]
    )
    return


@app.cell
def _(mo):
    mo.md(
        """
        ## 2. The honest counter-example

        A demo that only shows the fast case teaches the wrong lesson. This
        one touches every row on purpose. It is **not** sub-second, it is not
        supposed to be, and the service says which resource it spent —
        that is what the bottleneck classification in the benchmark suite is
        for.
        """
    )
    return


@app.cell
def _(mo):
    run_aggregate = mo.ui.run_button(label="run the full-table aggregate")
    run_aggregate
    return (run_aggregate,)


@app.cell
def _(TAP, httpx, mo, run_aggregate, time):
    if not run_aggregate.value:
        _out = mo.md("_Not run — it is deliberately expensive._")
    else:
        _adql = """
        SELECT dataproduct_type, COUNT(*) AS n, AVG(t_exptime) AS mean_exptime
        FROM ivoa.obscore
        GROUP BY dataproduct_type
        """
        _t0 = time.perf_counter()
        _r = httpx.post(
            f"{TAP}/sync",
            data={"LANG": "ADQL", "QUERY": _adql, "RESPONSEFORMAT": "csv"},
            timeout=600,
        )
        _elapsed = time.perf_counter() - _t0
        _out = mo.vstack(
            [
                mo.md(f"```sql{_adql}```"),
                mo.stat(f"{_elapsed:.1f} s", label="wall clock", bordered=True),
                mo.md(f"```\n{_r.text.strip()}\n```"),
                mo.md(
                    "Seconds, not milliseconds, and correctly so: this reads the "
                    "whole table. PostgreSQL may use parallel workers for it "
                    "(`max_parallel_workers_per_gather` in the demo values), which "
                    "is why it is not proportionally worse than the cone search."
                ),
            ]
        )
    _out
    return


@app.cell
def _(mo):
    mo.md(
        """
        ## 3. Both data models, one query language

        The observatory data products and the software records are different
        pydantic models, flattened into different SQL schemas by the same
        generator — and both answer ADQL.
        """
    )
    return


@app.cell
def _(TAP, httpx, mo, pd):
    def _csv(adql: str) -> pd.DataFrame:
        r = httpx.post(
            f"{TAP}/sync",
            data={"LANG": "ADQL", "QUERY": adql, "RESPONSEFORMAT": "csv"},
            timeout=120,
        )
        r.raise_for_status()
        from io import StringIO

        return pd.read_csv(StringIO(r.text))

    _odp = _csv("""
        SELECT TOP 5 obs_collection, COUNT(*) AS products
        FROM ivoa.obscore GROUP BY obs_collection ORDER BY products DESC
    """)
    try:
        _sw = _csv("SELECT TOP 5 * FROM software.software")
        _sw_panel = mo.ui.table(_sw, selection=None)
    except Exception as exc:
        _sw_panel = mo.md(f"_No software records ingested yet ({type(exc).__name__})._")

    mo.hstack(
        [
            mo.vstack([mo.md("**ODP / ObsCore**"), mo.ui.table(_odp, selection=None)]),
            mo.vstack([mo.md("**Software discovery**"), _sw_panel]),
        ]
    )
    return


@app.cell
def _(mo):
    mo.md(
        """
        ## 4. Thousands of requests, and the fleet answering them

        The slider sets how many requests to send and how many at once. Watch
        the replica count in the panel below: the HorizontalPodAutoscaler adds
        API pods when their CPU passes 70% of request, and takes them away a
        minute after the load stops.
        """
    )
    return


@app.cell
def _(mo):
    concurrency = mo.ui.slider(
        1, 256, value=32, step=1, label="concurrent clients", show_value=True
    )
    total = mo.ui.slider(
        100, 20000, value=2000, step=100, label="requests to send", show_value=True
    )
    run_load = mo.ui.run_button(label="send them")
    mo.vstack([concurrency, total, run_load])
    return concurrency, run_load, total


@app.cell
def _(TAP, alt, concurrency, mo, pd, run_load, time, total):
    if not run_load.value:
        _panel = mo.md("_Idle._")
    else:
        import asyncio
        import random

        import httpx as _httpx

        _queries = [
            "SELECT TOP 50 obs_publisher_did, s_ra, s_dec FROM ivoa.obscore "
            f"WHERE 1 = INTERSECTS(s_region_geom, CIRCLE('ICRS', {ra:.1f}, {dec:.1f}, 0.4))"
            for ra, dec in [(random.uniform(0, 360), random.uniform(-80, 20)) for _ in range(64)]
        ]

        async def _drive():
            latencies, errors = [], 0
            limit = asyncio.Semaphore(concurrency.value)
            async with _httpx.AsyncClient(
                timeout=60, limits=_httpx.Limits(max_connections=concurrency.value + 8)
            ) as client:

                async def one(i):
                    nonlocal errors
                    async with limit:
                        t0 = time.perf_counter()
                        try:
                            r = await client.post(
                                f"{TAP}/sync",
                                data={
                                    "LANG": "ADQL",
                                    "QUERY": _queries[i % len(_queries)],
                                    "RESPONSEFORMAT": "csv",
                                },
                            )
                            if r.status_code != 200:
                                errors += 1
                        except Exception:
                            errors += 1
                        latencies.append((time.time(), time.perf_counter() - t0))

                started = time.perf_counter()
                await asyncio.gather(*(one(i) for i in range(total.value)))
                return latencies, errors, time.perf_counter() - started

        _lat, _errors, _wall = asyncio.run(_drive())
        _df = pd.DataFrame(_lat, columns=["t", "latency_s"])
        _p = _df["latency_s"].quantile([0.5, 0.95, 0.99])

        _panel = mo.vstack(
            [
                mo.hstack(
                    [
                        mo.stat(
                            f"{total.value / _wall:,.0f}", label="requests/second", bordered=True
                        ),
                        mo.stat(f"{1000 * _p[0.5]:.0f} ms", label="p50", bordered=True),
                        mo.stat(f"{1000 * _p[0.95]:.0f} ms", label="p95", bordered=True),
                        mo.stat(f"{_errors}", label="errors", bordered=True),
                    ]
                ),
                mo.ui.altair_chart(
                    alt.Chart(_df)
                    .mark_circle(opacity=0.25, size=14)
                    .encode(
                        x=alt.X("t:Q", title="time (s)", scale=alt.Scale(zero=False)),
                        y=alt.Y("latency_s:Q", title="latency (s)", scale=alt.Scale(type="log")),
                    )
                    .properties(height=220)
                ),
                mo.md(
                    "A rising p95 with a flat request rate is queueing, not slowness: "
                    "the fleet is at its ceiling and the HPA has not caught up yet. "
                    "That gap is the thing worth watching."
                ),
            ]
        )
    _panel
    return


@app.cell
def _(mo):
    mo.md(
        """
        ## 5. What the cluster did about it

        Read straight from the Prometheus the chart deploys — the same series
        an operator would look at, and the same one Headlamp shows beside this
        notebook.
        """
    )
    return


@app.cell
def _(mo):
    refresh = mo.ui.refresh(default_interval="5s", label="refresh")
    window = mo.ui.dropdown(
        {"5 minutes": "5m", "15 minutes": "15m", "1 hour": "1h"},
        value="15 minutes",
        label="window",
    )
    mo.hstack([refresh, window])
    return refresh, window


@app.cell
def _(PROM, alt, httpx, mo, pd, refresh, window):
    refresh  # the dependency that makes this cell re-run on the timer

    def _range(query: str, span: str, step: str = "5s") -> pd.DataFrame:
        import time as _t

        end = _t.time()
        seconds = {"5m": 300, "15m": 900, "1h": 3600}[span]
        r = httpx.get(
            f"{PROM}/api/v1/query_range",
            params={"query": query, "start": end - seconds, "end": end, "step": step},
            timeout=20,
        )
        r.raise_for_status()
        rows = []
        for series in r.json()["data"]["result"]:
            label = series["metric"].get("pod") or series["metric"].get("__name__", "value")
            for ts, value in series["values"]:
                rows.append(
                    {
                        "t": pd.to_datetime(float(ts), unit="s"),
                        "series": label,
                        "value": float(value),
                    }
                )
        return pd.DataFrame(rows)

    def _chart(df, title, y_title):
        if df.empty:
            return mo.md(f"_No data for **{title}** in this window._")
        return mo.ui.altair_chart(
            alt.Chart(df)
            .mark_line()
            .encode(
                x=alt.X("t:T", title=""),
                y=alt.Y("value:Q", title=y_title),
                color=alt.Color("series:N", legend=None),
            )
            .properties(title=title, height=170)
        )

    try:
        _replicas = _range(
            'sum(kube_deployment_status_replicas_ready{deployment=~".*tap-api.*"}) '
            'or sum(up{job=~".*tap-api.*"})',
            window.value,
        )
        _rate = _range("sum(rate(tap_requests_total[1m]))", window.value)
        _cpu = _range(
            'sum(rate(process_cpu_seconds_total{job=~".*tap-api.*"}[1m])) by (pod)',
            window.value,
        )
        _panel = mo.vstack(
            [
                _chart(_replicas, "API pods ready", "pods"),
                _chart(_rate, "requests/second", "rps"),
                _chart(_cpu, "CPU cores in use, per pod", "cores"),
            ]
        )
    except Exception as exc:
        _panel = mo.md(
            f"_Prometheus is not reachable at `{PROM}` ({type(exc).__name__})._\n\n"
            "It is exposed only when `ingress.exposePrometheus` is on — the "
            "demo values file sets it, the chart default does not."
        )
    _panel
    return


@app.cell
def _(mo):
    mo.md(
        """
        ---

        **What this showed.** One deployment, on a cluster, answering a
        laptop somewhere else: a spatial query over a hundred gigabytes in
        milliseconds because of an index; a full-table aggregate in seconds
        because there is no index that helps; two independent metadata models
        under one query language; and a fleet that grows and shrinks with the
        load, measured by the same Prometheus the operator reads.

        The numbers here are one session on one cluster. The reproducible
        ones, with intervals and provenance, are in `docs/performance`.
        """
    )
    return


if __name__ == "__main__":
    app.run()
