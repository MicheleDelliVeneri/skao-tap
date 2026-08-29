"""egernia: the whole service, from another machine.

A marimo notebook. Reactive by design, so the sliders re-run only what depends
on them — turn concurrency up and the latency plot redraws while the Prometheus
panel keeps streaming.

    make notebook BASE_URL=https://egernia.test PROMETHEUS_URL=...

Points at whatever deployment BASE_URL names. In the SRCNet deployment stack
that is the ingress host from the dev overlay; locally it is docker-compose.

It runs in four parts: what a client can discover about the service, the four
ways to ask it a question, both metadata models under one query language, and
what happens when a thousand clients ask at once.

Nothing here is cached or pre-baked: every number is measured against the
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
    import auth as egernia_auth
    import httpx
    import marimo as mo
    import pandas as pd

    BASE = egernia_auth.base_url()
    TAP = f"{BASE}/tap"
    API = f"{BASE}/api/v1"
    # No default: the chart deploys no Prometheus, so the one holding these
    # series belongs to the deployment. In the dev stack that is
    # http://prometheus.test (or prometheus-operated.monitoring:9090 from
    # inside the cluster). Unset, the metrics panel says so and the rest of
    # the notebook still works.
    PROM = os.environ.get("EGERNIA_PROMETHEUS_URL", "").rstrip("/")

    # A dev cluster serves a certificate from its own CA, which httpx would
    # refuse. Set EGERNIA_INSECURE_TLS=1 only when the route to the service is
    # already an encrypted and authenticated channel, such as an SSH tunnel;
    # the honest fix is to trust the cluster's CA locally.
    VERIFY = os.environ.get("EGERNIA_INSECURE_TLS", "0") not in ("1", "true", "yes")

    # Every request carries the token: with auth on, TAP reads need one too
    # unless the deployment sets anonymousQueries, and the dev stack does not.
    # Failing here rather than per-cell is deliberate — one legible error about
    # credentials beats a page of 401s.
    AUTH, TOKEN_FROM = egernia_auth.auth_header()
    http = httpx.Client(verify=VERIFY, headers=AUTH)
    return API, AUTH, BASE, PROM, TAP, TOKEN_FROM, VERIFY, alt, http, mo, os, pd, time


@app.cell
def _(BASE, TAP, TOKEN_FROM, http, mo):
    # One reachability check, stated plainly: everything below is meaningless
    # if this fails, and "connection refused" three cells later reads as a
    # broken service rather than a DNS entry nobody made.
    try:
        _probe = http.get(f"{TAP}/availability", timeout=10)
        _reachable = _probe.status_code == 200
        _detail = f"HTTP {_probe.status_code}"
    except Exception as exc:
        _reachable, _detail = False, f"{type(exc).__name__}: {exc}"

    # A failed probe used to give one piece of advice -- check DNS -- which is
    # wrong for the failure this deployment actually produces. The cluster
    # serves a single certificate covering every `.test` host it knows about,
    # so a host missing from that list resolves, connects, and is refused at
    # the TLS handshake. "Check that the host resolves" sends the reader to
    # look at something that was never broken.
    if _reachable:
        _advice = ""
    elif "CERTIFICATE_VERIFY_FAILED" in _detail or "SSL" in _detail:
        _advice = (
            f"The host resolves and the ingress answered — this is a **certificate** "
            f"problem, not a network one, so checking DNS will not help. The cluster "
            f"serves one certificate for all of its `.test` hosts, and this one is not "
            f"among its names.\n\n"
            f"To carry on now: `EGERNIA_BASE_URL={BASE.replace('https://', 'http://', 1)}`, "
            f"or keep https with `EGERNIA_INSECURE_TLS=1`. Neither touches "
            f"`EGERNIA_AAPI_INSECURE_TLS`, which governs the leg that carries the "
            f"token and should stay on.\n\n"
            f"The lasting fix is to add the host to the cluster certificate's "
            f"`dnsNames` and let cert-manager reissue — note that a name present in "
            f"the manifest but absent from the issued secret still reports "
            f"`Ready=True`, because cert-manager is comparing against the live "
            f"resource rather than the file."
        )
    else:
        _advice = (
            "This machine cannot see the service. Check that the host in "
            "EGERNIA_BASE_URL resolves to the ingress address."
        )

    mo.md(
        f"""
        # egernia — the whole service, from another machine

        Service: `{BASE}` — {"**reachable**" if _reachable else f"**NOT reachable**, {_detail}"}

        Credential: {TOKEN_FROM}.

        {_advice}

        1. what a client can **discover**
        2. the four ways to **ask** it something
        3. both **metadata models**, one query language
        4. what happens at **scale**
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        """
        ---
        ## 1. What a client can discover

        A VO client arrives knowing only a URL. Everything it needs — the
        tables, the ADQL accepted, the formats written, the limits enforced —
        it reads from the service itself.
        """
    )
    return


@app.cell
def _(BASE, TAP, http, mo):
    _endpoints = [
        ("service root", f"{BASE}/", "what this is, and where everything else is"),
        ("OpenAPI", f"{BASE}/openapi.json", "the JSON API, machine-readable"),
        ("Swagger UI", f"{BASE}/docs", "the same, for a human"),
        ("liveness", f"{BASE}/health/live", "the process is up"),
        ("readiness", f"{BASE}/health/ready", "and can reach its database"),
        ("VOSI availability", f"{TAP}/availability", "IVOA: is the service up"),
        ("VOSI capabilities", f"{TAP}/capabilities", "IVOA: what it implements"),
        ("VOSI tables", f"{TAP}/tables", "IVOA: every table and column"),
        ("DALI examples", f"{TAP}/examples", "queries the service suggests"),
        ("VOResource", f"{TAP}/registry", "registry record (off unless configured)"),
        ("metrics", f"{BASE}/metrics", "Prometheus exposition"),
    ]
    _rows = []
    for _name, _url, _why in _endpoints:
        try:
            _resp = http.get(_url, timeout=20)
            _status, _size = _resp.status_code, len(_resp.content)
        except Exception as exc:
            _status, _size = type(exc).__name__, 0
        _rows.append(
            {
                "endpoint": _name,
                "path": _url.replace(BASE, "") or "/",
                "status": _status,
                "bytes": _size,
                "what it is for": _why,
            }
        )

    mo.vstack(
        [
            mo.md("### Every discovery endpoint, answered live"),
            mo.ui.table(_rows, selection=None, page_size=12),
            mo.md(
                "A 404 on the VOResource record is expected unless the deployment "
                "has an IVOA authority configured: an identifier promises that a "
                "URI resolves to this service forever, so it cannot be defaulted."
            ),
        ]
    )
    return


@app.cell
def _(TAP, http, mo):
    # The capabilities document is the contract. Rather than dump the XML,
    # pull out the parts a client actually branches on.
    _caps = http.get(f"{TAP}/capabilities", timeout=20).text

    def _inner(tag):
        out, rest = [], _caps
        needle = f"<{tag}"
        while needle in rest:
            rest = rest.split(needle, 1)[1]
            if ">" not in rest:
                break
            body = rest.split(">", 1)[1].split(f"</{tag}>", 1)[0]
            if body.strip() and "<" not in body:
                out.append(body.strip())
        return out

    _langs = _inner("version")
    _formats = _inner("mime")
    _models = _inner("dataModel")
    _uploads = _caps.count("uploadMethod")

    mo.vstack(
        [
            mo.md("### What the capabilities document promises"),
            mo.hstack(
                [
                    mo.stat(", ".join(_langs) or "—", label="ADQL versions", bordered=True),
                    mo.stat(str(len(_formats)), label="output formats", bordered=True),
                    mo.stat(str(_uploads), label="upload methods", bordered=True),
                ]
            ),
            mo.md(
                f"**Formats**: {', '.join(_formats) or '—'}\n\n"
                f"**Data models**: {', '.join(_models) or 'none declared'}\n\n"
                "ObsCore appearing here is what makes this service discoverable as "
                "an image archive by clients that never read its table list."
            ),
        ]
    )
    return


@app.cell
def _(API, http, mo):
    # What the deployment enforces, said by the deployment. A client should
    # not have to discover by trial that it needs a token.
    _auth = http.get(f"{API}/auth", timeout=15).json()
    _gated = _auth.get("gated_operations") or {}
    mo.vstack(
        [
            mo.md("### What this deployment enforces"),
            mo.md(
                f"- authentication: **{'on' if _auth.get('enabled') else 'off'}**\n"
                f"- gated operations: **{len(_gated) or 'none'}**"
            ),
            mo.ui.table([{"operation": k, "covers": v} for k, v in _gated.items()], selection=None)
            if _gated
            else mo.md(
                "_Nothing is gated: this demo runs open, so every request below is "
                "anonymous. With authentication on, the same endpoints need a "
                "bearer token and this table says which._"
            ),
        ]
    )
    return


@app.cell
def _(mo):
    mo.md(
        """
        ---
        ## 2. Four ways to ask the same question

        The same ADQL, through the client a VO astronomer already has, through
        raw HTTP, through the JSON API a pipeline would use, and as an
        asynchronous job. One engine, one job store, four interfaces.
        """
    )
    return


@app.cell
def _(mo):
    query_adql = mo.ui.text_area(
        value=(
            "SELECT TOP 20 obs_publisher_did, dataproduct_type, s_ra, s_dec, t_exptime\n"
            "FROM ivoa.obscore\n"
            "WHERE dataproduct_type = 'image'"
        ),
        label="the query every interface below will run",
        rows=4,
        full_width=True,
    )
    query_adql
    return (query_adql,)


@app.cell
def _(TAP, mo, query_adql, time):
    # (a) PyVO — the VO-native path: the client an astronomer already has, and
    # the one that shows the service is standards-compliant rather than merely
    # HTTP-shaped.
    try:
        import pyvo

        _t0 = time.perf_counter()
        _svc = pyvo.dal.TAPService(TAP)
        _table = _svc.search(query_adql.value).to_table()
        _elapsed = time.perf_counter() - _t0
        _panel = mo.vstack(
            [
                mo.md("### (a) PyVO — `pyvo.dal.TAPService`"),
                mo.hstack(
                    [
                        mo.stat(f"{1000 * _elapsed:.0f} ms", label="wall clock", bordered=True),
                        mo.stat(f"{len(_table):,}", label="rows", bordered=True),
                        mo.stat(str(len(_table.colnames)), label="columns", bordered=True),
                    ]
                ),
                mo.md(f"```\n{str(_table)[:1200]}\n```"),
                mo.md(
                    "PyVO parsed the VOTable, its units and its UCDs without being "
                    "told anything about this service beyond its URL."
                ),
            ]
        )
    except Exception as exc:
        _panel = mo.md(f"### (a) PyVO\n\n_Unavailable: {type(exc).__name__}: {exc}_")
    _panel
    return


@app.cell
def _(TAP, http, mo, pd, query_adql, time):
    # (b) Raw TAP: an HTTP form post. What PyVO does underneath, and what any
    # language with an HTTP client can do without a VO library at all.
    from io import StringIO

    _t0 = time.perf_counter()
    _r = http.post(
        f"{TAP}/sync",
        data={"LANG": "ADQL", "QUERY": query_adql.value, "RESPONSEFORMAT": "csv"},
        timeout=120,
    )
    _elapsed = time.perf_counter() - _t0
    _df = pd.read_csv(StringIO(_r.text)) if _r.status_code == 200 else pd.DataFrame()

    mo.vstack(
        [
            mo.md("### (b) Raw TAP — `POST /tap/sync`"),
            mo.hstack(
                [
                    mo.stat(f"{1000 * _elapsed:.0f} ms", label="wall clock", bordered=True),
                    mo.stat(f"{len(_df):,}", label="rows", bordered=True),
                    mo.stat(str(_r.status_code), label="HTTP", bordered=True),
                ]
            ),
            mo.ui.table(_df, selection=None, page_size=5),
            mo.md(
                "`RESPONSEFORMAT` picks the writer — votable, csv, tsv, json, "
                "parquet or arrow. The rows are identical; only the bytes differ."
            ),
        ]
    )
    return


@app.cell
def _(API, http, mo, query_adql, time):
    # (c) The JSON API: no XML, no VO library, an OpenAPI schema. What a
    # pipeline or a web front end would use.
    _t0 = time.perf_counter()
    _r = http.post(
        f"{API}/query",
        json={"query": query_adql.value, "lang": "ADQL", "format": "json"},
        timeout=120,
    )
    _elapsed = time.perf_counter() - _t0
    _body = _r.json() if _r.status_code == 200 else {}
    _meta = _body.get("metadata", [])

    mo.vstack(
        [
            mo.md("### (c) JSON API — `POST /api/v1/query`"),
            mo.hstack(
                [
                    mo.stat(f"{1000 * _elapsed:.0f} ms", label="wall clock", bordered=True),
                    mo.stat(f"{len(_body.get('data', [])):,}", label="rows", bordered=True),
                    mo.stat(str(_body.get("status", "—")), label="status", bordered=True),
                ]
            ),
            mo.md(
                "The response carries its own column metadata — name, datatype, "
                "unit, UCD — so a caller with no VOTable parser still knows that "
                "`t_exptime` is seconds and `s_ra` is degrees:"
            ),
            mo.ui.table(_meta, selection=None, page_size=6),
        ]
    )
    return


@app.cell
def _(mo):
    run_async = mo.ui.run_button(label="submit an asynchronous job, both ways")
    mo.vstack(
        [
            mo.md(
                "### (d) Asynchronous — the same query as a job\n\n"
                "A long query should not hold an HTTP connection open. UWS is the "
                "IVOA's answer and the JSON API mirrors it; both drive the **same "
                "job store**, so a job created through one is visible through the "
                "other."
            ),
            run_async,
        ]
    )
    return (run_async,)


@app.cell
def _(API, TAP, http, mo, query_adql, run_async, time):
    if not run_async.value:
        _panel = mo.md("_Not submitted._")
    else:
        _log = []

        # --- UWS (IVOA): create pending, then drive the phase
        _t0 = time.perf_counter()
        _create = http.post(
            f"{TAP}/async",
            data={"LANG": "ADQL", "QUERY": query_adql.value, "RESPONSEFORMAT": "csv"},
            follow_redirects=False,
            timeout=30,
        )
        _job_url = _create.headers.get("location", "")
        _log.append(f"UWS   POST /tap/async      -> {_create.status_code}, job at {_job_url}")
        if _job_url:
            http.post(
                f"{_job_url}/phase", data={"PHASE": "RUN"}, follow_redirects=False, timeout=30
            )
            _phase = "UNKNOWN"
            for _ in range(120):
                _phase = http.get(f"{_job_url}/phase", timeout=15).text.strip()
                if _phase in ("COMPLETED", "ERROR", "ABORTED"):
                    break
                time.sleep(0.5)
            _log.append(
                f"UWS   phase                -> {_phase} in {time.perf_counter() - _t0:.1f}s"
            )
            if _phase == "COMPLETED":
                _res = http.get(f"{_job_url}/results/result", timeout=60)
                _log.append(f"UWS   result               -> {len(_res.content):,} bytes")

        # --- JSON API: one POST creates and runs it
        _t1 = time.perf_counter()
        _job = http.post(
            f"{API}/jobs",
            json={"query": query_adql.value, "format": "csv", "run": True},
            timeout=30,
        ).json()
        _jid = _job.get("job_id", "")
        _log.append(f"JSON  POST /api/v1/jobs    -> {_jid} ({_job.get('phase')})")
        _state = _job
        for _ in range(120):
            _state = http.get(f"{API}/jobs/{_jid}", timeout=15).json()
            if _state.get("phase") in ("COMPLETED", "ERROR", "ABORTED"):
                break
            time.sleep(0.5)
        _log.append(
            f"JSON  phase                -> {_state.get('phase')} in "
            f"{time.perf_counter() - _t1:.1f}s"
        )
        if _state.get("phase") == "COMPLETED":
            _r = http.get(f"{API}/jobs/{_jid}/result", timeout=60)
            _log.append(f"JSON  result               -> {len(_r.content):,} bytes")
            _log.append(f"BOTH  the same job in UWS  -> {_state['urls']['uws']}")

        _panel = mo.vstack(
            [
                mo.md("```\n" + "\n".join(_log) + "\n```"),
                mo.md(
                    "The last line is the point: one job store, two protocols. A "
                    "pipeline submits over JSON and an astronomer watches the same "
                    "job in a VO client."
                ),
            ]
        )
    _panel
    return


@app.cell
def _(mo):
    show_errors = mo.ui.run_button(label="send three bad requests")
    mo.vstack(
        [
            mo.md(
                "### How it refuses\n\n"
                "An interface is defined as much by its failures. Each of these is "
                "a *usage* error and is answered as one — a DALI error document "
                "with a 4xx, not a 500 with a stack trace."
            ),
            show_errors,
        ]
    )
    return (show_errors,)


@app.cell
def _(TAP, http, mo, show_errors):
    if not show_errors.value:
        _panel = mo.md("_Not sent._")
    else:
        _cases = [
            ("syntax", "SELEC nonsense FROM nowhere"),
            ("unpublished table", "SELECT * FROM secret.table"),
            (
                "text column in a geometry predicate",
                "SELECT TOP 1 obs_id FROM ivoa.obscore "
                "WHERE 1 = INTERSECTS(s_region, CIRCLE('ICRS', 150, -30, 1))",
            ),
        ]
        _rows = []
        for _label, _adql in _cases:
            _r = http.post(
                f"{TAP}/sync",
                data={"LANG": "ADQL", "QUERY": _adql, "RESPONSEFORMAT": "csv"},
                timeout=60,
            )
            _text = _r.text
            _msg = _text.split("<INFO")[-1][:220] if "<INFO" in _text else _text[:220]
            _rows.append({"case": _label, "HTTP": _r.status_code, "the service says": _msg.strip()})
        _panel = mo.vstack(
            [
                mo.ui.table(_rows, selection=None),
                mo.md(
                    "The third is worth reading: `s_region` is the ObsCore standard "
                    "column and holds STC-S *text*, so it cannot be used in a "
                    "geometry predicate. The service names the column that can be "
                    "(`s_region_geom`) instead of letting PostgreSQL fail with "
                    "`operator does not exist: text && scircle`."
                ),
            ]
        )
    _panel
    return


@app.cell
def _(mo):
    mo.md(
        """
        ---
        ## 3. Two metadata models, one query language

        Observatory data products and software records are different pydantic
        models, flattened into SQL by the same generator, registered in the
        same `TAP_SCHEMA`, queried with the same ADQL. Neither needed a line of
        hand-written schema.
        """
    )
    return


@app.cell
def _(TAP, http, mo, pd):
    from io import StringIO as _S

    def _csv(adql: str) -> pd.DataFrame:
        r = http.post(
            f"{TAP}/sync",
            data={"LANG": "ADQL", "QUERY": adql, "RESPONSEFORMAT": "csv"},
            timeout=120,
        )
        r.raise_for_status()
        return pd.read_csv(_S(r.text))

    def _try(adql, label):
        try:
            return mo.vstack([mo.md(label), mo.ui.table(_csv(adql), selection=None, page_size=5)])
        except Exception as exc:
            return mo.md(f"{label}\n\n_Nothing ingested yet ({type(exc).__name__})._")

    _odp = _try(
        """
        SELECT TOP 10 o.collection, p.dataproduct_type, COUNT(*) AS products
        FROM srcnet.data_products AS p
        JOIN srcnet.observations AS o
          ON o.project_id = p.project_id AND o.obs_id = p.obs_id
        GROUP BY o.collection, p.dataproduct_type
        ORDER BY products DESC
        """,
        "**Observatory data products** — a join up the hierarchy",
    )
    _sw = _try(
        "SELECT TOP 10 uri, status, resources_requires_gpu, resources_min_memory"
        " FROM srcnet.software ORDER BY uri",
        "**Software discovery** — a different model, same ADQL",
    )
    mo.hstack([_odp, _sw], widths=[1, 1])
    return


@app.cell
def _(API, http, mo, pd):
    # TAP_SCHEMA is the map both models registered themselves in.
    _tables = http.get(f"{API}/tables", timeout=30).json()["tables"]
    _rows = [
        {
            "table": f"{t['schema']}.{t['name']}",
            "columns": len(t["columns"]),
            "description": (t["description"] or "")[:80],
        }
        for t in _tables
    ]
    mo.vstack(
        [
            mo.md("### Everything registered in TAP_SCHEMA"),
            mo.ui.table(pd.DataFrame(_rows), selection=None, page_size=8),
            mo.md(
                "`ivoa.obscore` is a view generated over the ODP tables, which is "
                "why the same rows are discoverable both as SKA data products and "
                "as standard ObsCore records."
            ),
        ]
    )
    return


@app.cell
def _(mo):
    mo.md(
        """
        ---
        ## 4. At scale

        A large table, a selective query and a fast answer — then the
        counter-example, then a thousand clients at once.
        """
    )
    return


@app.cell
def _(TAP, http, mo):
    # The demo's own database always has the ODP plugin's view, whose
    # `s_region_geom` is a pgsphere polygon with a GiST index. The probe stays
    # because a deployment can publish an ObsCore this notebook did not load —
    # its own archive's, or one left by the benchmark suite — and those carry
    # `s_region` as text with the index on the position instead.
    #
    # Decided by *asking the service*, not by reading TAP_SCHEMA. Where the
    # plugin finds a pre-existing obscore relation it leaves it alone — rightly
    # — but still registers its own column list, so TAP_SCHEMA can advertise
    # columns the relation does not have. Trusting it produced exactly the 500
    # this probe exists to avoid.
    def _works(predicate: str) -> bool:
        try:
            return (
                http.post(
                    f"{TAP}/sync",
                    data={
                        "LANG": "ADQL",
                        "QUERY": f"SELECT TOP 1 obs_id FROM ivoa.obscore WHERE {predicate}",
                        "RESPONSEFORMAT": "csv",
                    },
                    timeout=60,
                ).status_code
                == 200
            )
        except Exception:
            return False

    _circle = "CIRCLE('ICRS', 150.0, -30.0, 0.1)"
    HAS_FOOTPRINT = _works(f"1 = INTERSECTS(s_region_geom, {_circle})")

    def cone(ra: float, dec: float, radius: float) -> str:
        """The spatial predicate this deployment can actually answer.

        CONTAINS rather than INTERSECTS for the point form, and not merely for
        tidiness: pg_sphere 1.5.2 defines no `&&(spoint, scircle)`, so
        INTERSECTS(POINT, CIRCLE) resolves by implicit cast to two candidates
        and fails as `operator is not unique`.
        """
        circle = f"CIRCLE('ICRS', {ra}, {dec}, {radius})"
        if HAS_FOOTPRINT:
            return f"1 = INTERSECTS(s_region_geom, {circle})"
        return f"1 = CONTAINS(POINT('ICRS', s_ra, s_dec), {circle})"

    mo.md(
        "### Which spatial column this deployment can answer on\n\n"
        + (
            "`s_region_geom` — a pgsphere footprint with a GiST index, so the "
            "cone searches below use `INTERSECTS` over the polygon."
            if HAS_FOOTPRINT
            else "No usable `s_region_geom`: this deployment publishes an "
            "ObsCore whose `s_region` is text, with the index on the position. "
            "The cone searches below use `CONTAINS(POINT(s_ra, s_dec), ...)`, "
            "which is what that index answers."
        )
    )
    return (cone,)


@app.cell
def _(TAP, http, mo, pd):
    # Where to point the cone search, asked of the data instead of written
    # into the notebook.
    #
    # It used to be a fixed (150, -30), which returned *zero rows* on this
    # deployment — the seeded sky is clustered rather than uniform, so a
    # position picked by hand lands in a hole and the panel below reports 0
    # rows, 0 KiB, under prose explaining how the result grows with radius.
    # A different seed puts the holes somewhere else, so no fixed position is
    # safe; the only centre guaranteed to have data near it is one the data
    # supplied. Any row will do, and clustering is what makes that true:
    # measured across 20 sampled row positions on this deployment, the
    # *least* populated had 55 neighbours within 0.05 degrees.
    from io import StringIO as _SIO

    _probe = http.post(
        f"{TAP}/sync",
        data={
            "LANG": "ADQL",
            "QUERY": "SELECT TOP 1 s_ra, s_dec FROM ivoa.obscore",
            "RESPONSEFORMAT": "csv",
        },
        timeout=60,
    )
    try:
        _first = pd.read_csv(_SIO(_probe.text)).iloc[0]
        CENTRE = (round(float(_first["s_ra"]), 3), round(float(_first["s_dec"]), 3))
        _centre_note = (
            f"Centred on `({CENTRE[0]}, {CENTRE[1]})`, taken from the first row the "
            "service returns — so the searches below have something to find whatever "
            "sky this deployment was seeded with."
        )
    except Exception:
        # An empty or unreadable answer is not a reason to stop: the cells
        # below still demonstrate the query path, they just find nothing.
        CENTRE = (150.0, -30.0)
        _centre_note = (
            "Could not read a position from `ivoa.obscore`, so the cone searches "
            f"below use a fixed `({CENTRE[0]}, {CENTRE[1]})` and may well return "
            "nothing. Check that the dataset finished seeding."
        )
    mo.md("### Where to look\n\n" + _centre_note)
    return (CENTRE,)


@app.cell
def _(mo):
    radius = mo.ui.slider(
        0.05, 5.0, value=0.5, step=0.05, label="cone radius (degrees)", show_value=True
    )
    radius
    return (radius,)


@app.cell
def _(CENTRE, TAP, cone, http, mo, radius, time):
    _adql = (
        "SELECT TOP 1000 obs_publisher_did, s_ra, s_dec, em_min, em_max, access_url\n"
        "FROM ivoa.obscore\n"
        f"WHERE {cone(CENTRE[0], CENTRE[1], radius.value)}"
    )
    _t0 = time.perf_counter()
    _r = http.post(
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
                "the GiST index finds the candidates regardless of how much data it "
                "is not looking at, and what is left is the cost of writing rows out."
                if _elapsed < 1.0
                else "Slower than a second usually means a cold cache — the first "
                "query after a deploy reads from disk. Run it again."
            ),
        ]
    )
    return


@app.cell
def _(mo):
    big_rows = mo.ui.dropdown(
        options={"50,000": 50_000, "100,000": 100_000, "250,000": 250_000, "500,000": 500_000},
        value="250,000",
        label="rows to pull back",
    )
    big_rows
    return (big_rows,)


@app.cell
def _(TAP, big_rows, http, mo, time):
    # The previous cell is a needle: a selective predicate the index answers,
    # where the result is small and the work is the lookup. This one is the
    # opposite and is the honest test of a different thing entirely — pulling
    # a quarter of a million rows back is no longer about finding them, it is
    # about serialising and streaming them, and that cost is per row and
    # cannot be indexed away.
    #
    # Streamed, not buffered: the service writes rows out as it reads them, so
    # its memory does not scale with the result. Time to *first* byte is what
    # a client sees as responsiveness; time to last is bounded by the link.
    _n = big_rows.value
    _adql = (
        f"SELECT TOP {_n} obs_publisher_did, obs_collection, dataproduct_type, "
        "calib_level, s_ra, s_dec, t_min, t_max, em_min, em_max, access_estsize\n"
        "FROM ivoa.obscore"
    )
    _t0 = time.perf_counter()
    with http.stream(
        "POST",
        f"{TAP}/sync",
        data={"LANG": "ADQL", "QUERY": _adql, "RESPONSEFORMAT": "csv"},
        timeout=600,
    ) as _resp:
        _first = None
        _bytes = 0
        _rows = 0
        for _chunk in _resp.iter_bytes():
            if _first is None:
                _first = time.perf_counter() - _t0
            _bytes += len(_chunk)
            _rows += _chunk.count(b"\n")
    _elapsed = time.perf_counter() - _t0
    _rows = max(_rows - 1, 0)  # the header line

    mo.vstack(
        [
            mo.md(f"```sql\n{_adql}\n```"),
            mo.hstack(
                [
                    mo.stat(f"{1000 * (_first or 0):.0f} ms", label="first byte", bordered=True),
                    mo.stat(f"{_elapsed:.1f} s", label="last byte", bordered=True),
                    mo.stat(f"{_rows:,}", label="rows", bordered=True),
                    mo.stat(f"{_bytes / 2**20:.0f} MiB", label="streamed", bordered=True),
                    mo.stat(
                        f"{_rows / max(_elapsed, 1e-9):,.0f}/s", label="row rate", bordered=True
                    ),
                ]
            ),
            mo.md(
                f"First byte at {1000 * (_first or 0):.0f} ms against "
                f"{_elapsed:.1f} s to the last: the service starts answering long "
                "before it has finished, because the result is streamed rather than "
                "assembled in memory. Raising the row count moves the last-byte "
                "figure and leaves the first-byte one roughly where it is — which is "
                "the shape you want, and the reason a large result does not become a "
                "large heap."
            ),
        ]
    )
    return


@app.cell
def _(mo):
    run_aggregate = mo.ui.run_button(label="run the full-table aggregate")
    mo.vstack(
        [
            mo.md(
                "### The honest counter-example\n\n"
                "A demo that only shows the fast case teaches the wrong lesson. "
                "This one touches every row on purpose. It is **not** sub-second "
                "and is not supposed to be."
            ),
            run_aggregate,
        ]
    )
    return (run_aggregate,)


@app.cell
def _(TAP, http, mo, run_aggregate, time):
    if not run_aggregate.value:
        _out = mo.md("_Not run — it is deliberately expensive._")
    else:
        _adql = """
        SELECT dataproduct_type, COUNT(*) AS n, AVG(t_exptime) AS mean_exptime
        FROM ivoa.obscore
        GROUP BY dataproduct_type
        """
        _t0 = time.perf_counter()
        _r = http.post(
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
                    "Seconds, not milliseconds, and correctly so: no index helps a "
                    "query that reads everything. PostgreSQL may use parallel "
                    "workers, which is why it is not proportionally worse than the "
                    "cone search."
                ),
            ]
        )
    _out
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
    mo.vstack(
        [
            mo.md(
                "### A thousand clients at once\n\n"
                "Watch the pod count below: the autoscaler adds API pods when "
                "their CPU passes 70% of request, and takes them away a minute "
                "after the load stops."
            ),
            concurrency,
            total,
            run_load,
        ]
    )
    return concurrency, run_load, total


@app.cell
async def _(TAP, VERIFY, alt, concurrency, cone, mo, pd, run_load, time, total):
    if not run_load.value:
        _panel = mo.md("_Idle._")
    else:
        import asyncio
        import random

        import httpx as _httpx

        _queries = [
            "SELECT TOP 50 obs_publisher_did, s_ra, s_dec FROM ivoa.obscore "
            f"WHERE {cone(round(ra, 1), round(dec, 1), 0.4)}"
            for ra, dec in [(random.uniform(0, 360), random.uniform(-80, 20)) for _ in range(64)]
        ]

        async def _drive():
            latencies, errors = [], 0
            limit = asyncio.Semaphore(concurrency.value)
            async with _httpx.AsyncClient(
                timeout=60,
                verify=VERIFY,
                limits=_httpx.Limits(max_connections=concurrency.value + 8),
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

        # `await`, not `asyncio.run`: marimo runs cells inside its own event
        # loop, and asyncio.run refuses to nest ("cannot be called from a
        # running event loop"). marimo supports top-level await in a cell
        # declared `async def`, which is the whole fix -- verified against the
        # marimo in this deployment's singleuser image rather than assumed.
        _lat, _errors, _wall = await _drive()
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
                    "A rising p95 with a flat request rate is queueing, not "
                    "slowness: the fleet is at its ceiling and the autoscaler has "
                    "not caught up. That gap is the thing worth watching."
                ),
            ]
        )
    _panel
    return


@app.cell
def _(mo):
    refresh = mo.ui.refresh(default_interval="5s", label="refresh")
    window = mo.ui.dropdown(
        {"5 minutes": "5m", "15 minutes": "15m", "1 hour": "1h"},
        value="15 minutes",
        label="window",
    )
    mo.vstack(
        [
            mo.md(
                "### What the cluster did about it\n\n"
                "Read from the Prometheus the chart deploys — the same series an "
                "operator sees, and the same one Headlamp shows beside this notebook."
            ),
            mo.hstack([refresh, window]),
        ]
    )
    return refresh, window


@app.cell
def _(PROM, alt, http, mo, pd, refresh, window):
    refresh  # the dependency that makes this cell re-run on the timer

    def _range(query: str, span: str, step: str = "5s") -> pd.DataFrame:
        import time as _t

        end = _t.time()
        seconds = {"5m": 300, "15m": 900, "1h": 3600}[span]
        r = http.get(
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

    def _panels():
        return mo.vstack(
            [
                _chart(_range('sum(up{job=~".*tap-api.*"})', window.value), "API pods up", "pods"),
                _chart(
                    _range("sum(rate(tap_query_duration_seconds_count[1m]))", window.value),
                    "queries/second",
                    "qps",
                ),
                _chart(
                    _range(
                        'sum(rate(process_cpu_seconds_total{job=~".*tap-api.*"}[1m])) by (pod)',
                        window.value,
                    ),
                    "CPU cores in use, per pod",
                    "cores",
                ),
            ]
        )

    if not PROM:
        # No default to fall back on: the chart deploys no Prometheus, so the
        # one holding these series belongs to the deployment and only it knows
        # the URL.
        _panel = mo.md(
            "_No Prometheus configured._\n\n"
            "The chart deploys none: the metrics endpoints are scraped by "
            "whichever Prometheus the deployment runs. Set "
            "`EGERNIA_PROMETHEUS_URL` (`make notebook PROMETHEUS_URL=...`) to "
            "the one holding these series."
        )
    else:
        try:
            _panel = _panels()
        except Exception as exc:
            _panel = mo.md(
                f"_Prometheus is not reachable at `{PROM}` ({type(exc).__name__})._\n\n"
                "The series are exported by the pods and collected by the "
                "deployment's own Prometheus; check that it scrapes them and "
                "that this URL is the one to query."
            )
    _panel
    return


@app.cell
def _(mo):
    mo.md(
        """
        ---

        **What this showed.** One deployment on a cluster, answering a laptop
        somewhere else: a service that describes itself well enough for a VO
        client to use it knowing only a URL; the same query through PyVO, raw
        HTTP, JSON and as an asynchronous job, against one engine and one job
        store; failures that arrive as usage errors naming the column at fault;
        two independently-generated metadata models under one query language; a
        spatial query over a hundred gigabytes in milliseconds and a full-table
        aggregate in seconds, each for a reason; and a fleet that grows and
        shrinks with the load.

        The numbers here are one session on one cluster, without repetitions or
        intervals — an illustration, not a measurement. The reproducible ones,
        with confidence intervals and a bottleneck classification saying *which*
        resource ran out, are in `docs/performance`.
        """
    )
    return


if __name__ == "__main__":
    app.run()
