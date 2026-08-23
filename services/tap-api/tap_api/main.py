"""SKAO TAP service frontend.

Implements the TAP 1.1 endpoint set: /sync, /async (UWS 1.1), and the VOSI
resources /capabilities, /availability, /tables, plus DALI /examples.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from tapcore.auth import verifier
from tapcore.config import settings
from tapcore.db import close_pool, pool
from tapcore.errors import AuthenticationError, TAPError
from tapcore.metadata import ingest
from tapcore.metadata.plugins import active_plugins
from tapcore.query.votable import error_votable

from . import auth
from .endpoints import vosi
from .endpoints.json_api import router as json_router
from .endpoints.uws_api import router as uws_router
from .queries.params import gather_params
from .queries.query import prepare_query, run_sync
from .queries.uploads import gather_upload_files, parse_uploads, resolve_upload_sources

# uvicorn only configures its own loggers, so without this the service's own
# records (schema bootstrap, legacy-table warnings, the metadata deletion
# audit trail) never reach a handler above WARNING. The executor does the
# same; package 8 of the roadmap replaces both with ska-src-logging.
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

log = logging.getLogger("tap-api")


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
            with pool().connection() as conn, conn.transaction():
                # forward-migrate deployments whose uws.jobs predates ABORT
                # support (the column is in db/init for fresh databases)
                conn.execute("ALTER TABLE uws.jobs ADD COLUMN IF NOT EXISTS backend_pid integer")
                for plugin in plugins:
                    ingest.ensure_schema(conn, plugin)
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
    yield
    close_pool()


app = FastAPI(
    title="SKAO TAP service",
    version="0.1.0",
    lifespan=lifespan,
    # verify any bearer token on any route; the per-operation gates below
    # decide what a verified principal is then allowed to do
    dependencies=[Depends(auth.attach_principal)],
)
app.include_router(uws_router, prefix="/tap/async")
app.include_router(json_router)

VOTABLE_MIME = "application/x-votable+xml"


@app.exception_handler(TAPError)
async def tap_error_handler(request: Request, exc: TAPError):
    # DALI mandates VOTable error documents on the TAP endpoints; the JSON
    # API reports the same errors as JSON.
    headers = {}
    if isinstance(exc, AuthenticationError):
        # RFC 6750: a 401 has to say how to authenticate
        headers["WWW-Authenticate"] = 'Bearer realm="skao-tap"'
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": type(exc).__name__, "message": exc.message},
            status_code=exc.http_status,
            headers=headers,
        )
    return Response(
        error_votable(exc.message),
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
    """
    return Response(vosi.voresource_xml(), media_type="application/xml")


@app.get("/tap/availability")
async def availability():
    return Response(vosi.availability_xml(), media_type="application/xml")


@app.get("/tap/capabilities")
async def capabilities():
    return Response(vosi.capabilities_xml(), media_type="application/xml")


@app.get("/tap/tables")
async def tables():
    return Response(vosi.tables_xml(), media_type="application/xml")


@app.get("/tap/sync")
@app.post("/tap/sync")
async def sync(request: Request):
    params = await gather_params(request)
    if params.get("REQUEST") == "getCapabilities":  # TAP 1.0 compatibility
        return RedirectResponse(f"{settings.base_url}/capabilities", status_code=303)
    files = await gather_upload_files(request)
    uploads = parse_uploads(resolve_upload_sources(params.get("UPLOAD"), files))
    prepared = prepare_query(params)
    chunks, mime = run_sync(prepared, uploads)
    return StreamingResponse(chunks, media_type=mime)


@app.get("/tap/examples")
async def examples():
    body = """<!DOCTYPE html>
<html vocab="http://www.ivoa.net/rdf/examples#">
<head><title>TAP examples</title></head>
<body>
<h1>Service-provided examples (DALI)</h1>
<div typeof="example" id="cone" resource="#cone">
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
