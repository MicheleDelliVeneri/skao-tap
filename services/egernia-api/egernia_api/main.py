"""egernia TAP service frontend.

Implements the TAP 1.1 endpoint set: /sync, /async (UWS 1.1), and the VOSI
resources /capabilities, /availability, /tables, plus DALI /examples.
"""

import socket
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from egernia_core import uws
from egernia_core.auth import invalid_token_challenge, verifier
from egernia_core.config import base_url, request_origin, settings, trusted_hosts
from egernia_core.db import close_pool, pool
from egernia_core.db import connection as db_connection
from egernia_core.errors import AuthenticationError, OverloadedError, TAPError
from egernia_core.metadata import ingest
from egernia_core.metadata.plugins import active_plugins
from egernia_core.observability import (
    DB_POOL_EXHAUSTED,
    REGISTRY,
    REQUEST_ID_HEADER,
    configure_logging,
    new_request_id,
    request_context,
    safe_request_id,
)
from egernia_core.query.votable import error_votable
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg_pool import PoolTimeout
from ska_src_logging import LogContext
from ska_src_logging.integrations.fastapi import setup_otel_fastapi, setup_uvicorn_logging
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from . import auth
from .endpoints import vosi
from .endpoints.json_api import metadata_routers
from .endpoints.json_api import router as json_router
from .endpoints.uws_api import router as uws_router
from .queries.params import gather_params
from .queries.query import forget_published_tables, prepare_query, run_sync
from .queries.uploads import gather_upload_sources, parse_uploads

# Structured records with SRCNet's shared fields, JSON in a container and
# coloured console when a human is watching. This also configures uvicorn's
# own loggers, which otherwise keep their own format.
log = configure_logging("tap-api")
setup_uvicorn_logging("tap-api")


def _bootstrap_metadata(attempts: int = 5, delay_s: float = 2.0) -> None:
    """Create/refresh the tables generated from the active metadata plugins."""
    plugins = active_plugins()
    log.info("active metadata plugins: %s", ", ".join(p.name for p in plugins) or "none")
    # Resolve the policy and build the token verifier now, so a bad auth
    # configuration stops the pod at startup instead of turning every
    # request into a 500. Neither call touches the network: discovery and
    # JWKS are fetched on first use, so the service still starts when the
    # IAM is briefly unavailable.
    if auth.plugin() is not None:
        verifier()
    for attempt in range(1, attempts + 1):
        try:
            with db_connection() as conn, conn.transaction():
                # forward-migrate deployments whose uws.jobs predates ABORT
                # support (the columns are in db/init for fresh databases).
                # query_tables is ensured here as well as in the executor:
                # the API writes it at queue time, and in a rolling upgrade a
                # new API can queue a job before any new executor has started.
                uws.ensure_job_columns(conn)
                for plugin in plugins:
                    ingest.ensure_schema(conn, plugin)
                # Last, so it sees the finished schema. Reported rather than
                # enforced: a divergence usually means this service shares a
                # database with something that owns some of those relations,
                # which is legitimate — but a client cannot tell, and finds
                # out as a 500 on a query our own metadata recommended.
                ingest.warn_tap_schema_divergence(conn)
            return
        except Exception as exc:
            if attempt == attempts:
                # Fail fast: a half-initialized service would only surface
                # confusing errors later on the metadata endpoints and
                # generated-schema queries; the orchestrator should restart us.
                raise RuntimeError(f"metadata bootstrap failed after {attempts} attempts") from exc
            log.warning("metadata bootstrap attempt %d failed (%s), retrying", attempt, exc)
            time.sleep(delay_s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_metadata()
    # bootstrap is the one path where this service publishes tables itself;
    # anything published out of band is picked up by the cache's own expiry
    forget_published_tables()
    yield
    close_pool()


# Which pod answered. Kubernetes sets the hostname to the pod name, so this
# turns "which replica served this request" from an inference — a proxy
# measurement that on a deep queue reports the queue — into something each
# response states. Read once: it cannot change while the process lives.
SERVED_BY_HEADER = "X-Served-By"
SERVED_BY = socket.gethostname()

app = FastAPI(
    title="egernia TAP service",
    version="0.1.0",
    lifespan=lifespan,
    # verify any bearer token on any route; the per-operation gates below
    # decide what a verified principal is then allowed to do
    dependencies=[Depends(auth.attach_principal)],
)


def client_origin(request: Request) -> str | None:
    """``scheme://host`` as the client wrote it, when that host is trusted.

    Host is client-controlled, so an unvetted one would let a caller choose
    the URLs this service prints into its own job documents. An unlisted host
    falls back to the configured base URL rather than being refused: the
    kubelet probes reach the pod by IP and the executor by service name, and
    neither should start failing over the URLs in a document it never reads.

    The port is kept — a tunnel on :8080 is reachable only on :8080 — and the
    scheme follows X-Forwarded-Proto, since TLS terminates at the ingress and
    this process only ever sees http.
    """
    host = request.headers.get("host", "")
    if (urlsplit(f"//{host}").hostname or "") not in trusted_hosts():
        return None
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme.split(',')[0].strip()}://{host}"


@app.middleware("http")
async def correlate(request: Request, call_next):
    """Give every request an id, and put it where the logs and the database
    can see it.

    An id supplied by the caller is kept — a client or gateway that already
    traces a request should not have that broken here — and returned on the
    response so a caller can quote it when reporting a problem.

    The client's origin is bound here too, for the same reason and the same
    lifetime: every URL this request prints has to be one it can fetch back.
    """
    # a caller's id is kept only if it is plainly an identifier: it is echoed
    # in a header, written into a SQL comment and logged, so anything else
    # gets replaced rather than escaped
    rid = safe_request_id(request.headers.get(REQUEST_ID_HEADER)) or new_request_id()
    with (
        request_context(rid),
        request_origin(client_origin(request)),
        LogContext(request_id=rid, path=request.url.path),
    ):
        response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = rid
    response.headers[SERVED_BY_HEADER] = SERVED_BY
    return response


# Probes.
#
# Deliberately not /tap/availability, which is what they used to be pointed at
# and which is a VOSI resource that reports on the *database*. Under load the
# connection pool saturates, that endpoint queues for a connection, the probe's
# one-second default timeout expires, and Kubernetes kills a process that is
# busy rather than broken — turning an overload into an outage, and dropping
# every in-flight request with it. Measured: at an offered rate well inside the
# service's own closed-loop capacity, the API was SIGKILLed twice by its
# liveness probe.
#
# So the two questions are asked separately, because their remedies differ.
# Liveness asks "is this process wedged?", whose remedy is a restart, and it
# must therefore not depend on anything outside the process. Readiness asks
# "should this pod be sent traffic?", which does depend on the database — but a
# full pool is not an unreachable database, and treating it as one removes a
# working pod from the Service and concentrates the load on its peers.
@app.get("/health/live", include_in_schema=False)
async def live():
    """The process is running and the event loop is turning.

    Touches nothing else on purpose: no database, no disk, no pool. If this
    cannot answer, the process really is stuck and restarting it is right.
    """
    return Response("ok\n", media_type="text/plain")


@app.get("/health/ready", include_in_schema=False)
async def ready():
    """Whether this pod should be sent traffic.

    A short, non-queueing look at the database. The distinction that matters is
    between "the pool is busy" and "the database is gone": the first is a
    healthy service under load and must stay in the Service, the second is a
    pod that cannot serve and should leave it.
    """
    try:
        await run_in_threadpool(_probe_database)
    except PoolTimeout:
        # Every connection is in use. That is what a busy service looks like,
        # not a broken one — and taking this pod out of rotation now would push
        # its share of the load onto pods in exactly the same state.
        return Response("busy\n", media_type="text/plain")
    except Exception:
        # The reason goes to the log, not to the body. This endpoint is
        # reachable without a token by design, and a psycopg connection error
        # names the host, port and user it failed to reach — internal topology
        # that a probe response has no business publishing. The kubelet only
        # needs the status code.
        log.warning("readiness probe failed", exc_info=True)
        return Response("unavailable\n", status_code=503, media_type="text/plain")
    return Response("ready\n", media_type="text/plain")


def _probe_database() -> None:
    """One trivial statement, with its own short timeout.

    A separate timeout from the query path's: a probe that waits as long as a
    user query is a probe that reports on the queue rather than on the
    database.
    """
    with pool().connection(timeout=settings.health_probe_timeout_s) as conn:
        conn.execute("SELECT 1")


# /metrics rather than the library's default /v1/metrics: this service's own
# versioned namespace is /api/v1, and the metrics are not part of that API.
#
# Served as a route rather than through the library's helper, which mounts a
# sub-application and so answers /metrics with a 307 to /metrics/. Scrape
# configs and pod annotations say /metrics, and a scraper that does not follow
# redirects would see nothing. The exposition is identical — same registry.
@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


# Traces only when someone is collecting them, so a deployment without a
# collector pays nothing and needs no extra configuration.
if settings.otlp_endpoint:
    setup_otel_fastapi(app, service_name="tap-api", otlp_endpoint=settings.otlp_endpoint)
    log.info("OpenTelemetry traces exported to %s", settings.otlp_endpoint)

app.include_router(uws_router)
app.include_router(json_router)
# Each metadata domain on the app rather than nested inside json_router, so its
# routes report their full path to the authorisation layer.
for _metadata_router in metadata_routers:
    app.include_router(_metadata_router)

VOTABLE_MIME = "application/x-votable+xml"


@app.exception_handler(PoolTimeout)
async def pool_timeout_handler(request: Request, exc: PoolTimeout):
    """Every connection is busy: answer 503 rather than hanging or 500-ing.

    The pool is the service's concurrency limit, and reaching it is a capacity
    condition, not a fault. Retry-After lets a client or proxy back off instead
    of retrying immediately into the same wall.
    """
    DB_POOL_EXHAUSTED.inc()
    log.warning("connection pool exhausted serving %s", request.url.path)
    overloaded = OverloadedError(
        "all database connections are busy; retry shortly"
        " (raise TAP_DB_POOL_MAX, or add API workers and replicas)"
    )
    response = await tap_error_handler(request, overloaded)
    response.headers["Retry-After"] = "1"
    return response


@app.exception_handler(TAPError)
async def tap_error_handler(request: Request, exc: TAPError):
    # DALI mandates VOTable error documents on the TAP endpoints; the JSON
    # API reports the same errors as JSON.
    headers = {}
    message = exc.message
    if isinstance(exc, AuthenticationError):
        # RFC 6750 wants a challenge on a 401; IVOA AuthVO wants one that
        # names the IAM. Both go in the one header, in that order, so a
        # client reading only the first still learns it needs a bearer token.
        challenge = exc.challenge or invalid_token_challenge(exc.message)
        headers["WWW-Authenticate"] = f'Bearer realm="egernia", {challenge}'
        # and in the body too: that is where the SRCNet reference client (the
        # DM product streamer's) reads the challenge from
        message = f"{exc.message}. WWW-Authenticate: {challenge}"
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": type(exc).__name__, "message": message},
            status_code=exc.http_status,
            headers=headers,
        )
    return Response(
        error_votable(message),
        status_code=exc.http_status,
        media_type=VOTABLE_MIME,
        headers=headers,
    )


@app.get("/")
async def root():
    return RedirectResponse("/tap/capabilities")


@app.get("/tap/registry")
async def registry():
    """The VOResource record a publishing registry harvests or ingests.

    404 until the deployment is configured to publish one: an IVOA
    identifier is a permanent promise about a URI, so it cannot be defaulted.

    Errors are answered as plain text rather than through the DALI VOTable
    handler the TAP endpoints use. A harvester asking for a registry record
    has no reason to parse a VOTable, and answering one with the
    x-votable+xml content type would look like a malformed record rather than
    a missing one.
    """
    try:
        return Response(vosi.voresource_xml(), media_type="application/xml")
    except TAPError as exc:
        # exc.message, not str(exc): the message is ours (which chart value is
        # unset), while stringifying the exception is how implementation
        # detail leaks into a response
        return PlainTextResponse(exc.message, status_code=exc.http_status)


@app.get("/tap/availability")
async def availability():
    return Response(vosi.availability_xml(), media_type="application/xml")


@app.get("/tap/capabilities")
async def capabilities():
    return Response(vosi.capabilities_xml(), media_type="application/xml")


@app.get("/tap/tables")
async def tables():
    return Response(vosi.tables_xml(), media_type="application/xml")


# query.sync is off by default: a deployment that gates it refuses anonymous
# synchronous queries, which is most VO tooling. Gating jobs.create without it
# leaves synchronous querying open, so the two belong together.
@app.get("/tap/sync", dependencies=[Depends(auth.require("query.sync"))])
@app.post("/tap/sync", dependencies=[Depends(auth.require("query.sync"))])
async def sync(request: Request):
    params = await gather_params(request)
    if params.get("REQUEST") == "getCapabilities":  # TAP 1.0 compatibility
        return RedirectResponse(f"{base_url()}/capabilities", status_code=303)
    uploads = parse_uploads(await gather_upload_sources(request, params))
    # ADQL translation is tens of milliseconds of pure-Python ANTLR work; on
    # the event loop it stalls every other request for that long, which is why
    # throughput stopped rising with concurrency
    prepared = await run_in_threadpool(prepare_query, params)
    # run_sync takes a pool connection and produces the first chunk, and the
    # iterator does blocking reads for the rest. On the event loop that means
    # one slow query — or one wait for a busy pool — stalls every other
    # request in this worker, including ones ready to send.
    chunks, mime = await run_in_threadpool(run_sync, prepared, uploads)
    return StreamingResponse(iterate_in_threadpool(chunks), media_type=mime)


@app.get("/tap/examples")
async def examples():
    obscore_example = ""
    if vosi.obscore_active():
        obscore_example = """<div typeof="example" id="obscore-cone" resource="#obscore-cone">
  <h2 property="name">ObsCore: data products overlapping a cone</h2>
  <pre property="query">
SELECT obs_publisher_did, dataproduct_type, access_url
FROM ivoa.obscore
WHERE 1 = INTERSECTS(s_region_geom, CIRCLE('ICRS', 150.0, -30.0, 0.5))
  </pre>
</div>
"""
    body = f"""<!DOCTYPE html>
<html vocab="http://www.ivoa.net/rdf/examples#">
<head><title>TAP examples</title></head>
<body>
<h1>Service-provided examples (DALI)</h1>
{obscore_example}<div typeof="example" id="cone" resource="#cone">
  <h2 property="name">Cone search on the continuum catalogue</h2>
  <pre property="query">
SELECT source_id, source_name, ra, dec, flux_int
FROM ska.continuum_sources
WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 62.3, -65.5, 1.0))
  </pre>
</div>
<div typeof="example" id="brightest" resource="#brightest">
  <h2 property="name">Brightest sources</h2>
  <pre property="query">
SELECT TOP 5 source_name, flux_int FROM ska.continuum_sources ORDER BY flux_int DESC
  </pre>
</div>
<div typeof="example" id="metadata" resource="#metadata">
  <h2 property="name">List published tables via TAP_SCHEMA</h2>
  <pre property="query">SELECT table_name, description FROM tap_schema.tables</pre>
</div>
</body>
</html>
"""
    return HTMLResponse(body)
