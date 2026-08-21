"""SKAO TAP service frontend.

Implements the TAP 1.1 endpoint set: /sync, /async (UWS 1.1), and the VOSI
resources /capabilities, /availability, /tables, plus DALI /examples.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from tapcore.config import settings
from tapcore.db import close_pool
from tapcore.errors import TAPError
from tapcore.votable import error_votable

from . import vosi
from .params import gather_params
from .query import prepare_query, run_sync
from .uws_api import router as uws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    close_pool()


app = FastAPI(title="SKAO TAP service", version="0.1.0", lifespan=lifespan)
app.include_router(uws_router, prefix="/tap/async")

VOTABLE_MIME = "application/x-votable+xml"


@app.exception_handler(TAPError)
async def tap_error_handler(request: Request, exc: TAPError):
    return Response(
        error_votable(exc.message), status_code=exc.http_status, media_type=VOTABLE_MIME
    )


@app.get("/")
async def root():
    return RedirectResponse("/tap/capabilities")


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
    prepared = prepare_query(params)
    body, mime = run_sync(prepared)
    return Response(body, media_type=mime)


@app.get("/tap/examples")
async def examples():
    body = f"""<!DOCTYPE html>
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
