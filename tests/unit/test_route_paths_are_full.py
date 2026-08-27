"""The route the authorisation layer reports must be the route it documents.

`AuthMiddleware` sends the Permissions API `request.scope["route"].path` as the
route being asked about, and policies are written against the OpenAPI document.
Those two agree only if every route reports its full path.

They did not. FastAPI keeps included routers nested rather than flattening them
(`fastapi.routing._IncludedRouter`), and sets `scope["route"]` to the *original*
route object, whose `.path` is relative to whichever router declared it — so a
prefix passed to `include_router`, or an outer prefix on a nested include, is
absent from the value sent to PAPI while OpenAPI shows it. `POST /tap/async`
reported `''`; `/api/v1/software` reported `/software`. `scope["root_path"]` is
empty, so nothing in the request can put the prefix back.

Only metadata mutation is gated by default, which is why this stayed invisible:
PAPI was never asked about the UWS routes whose keys were wrong. Widening
`gatedOperations` would have denied every job request against a policy that
looked correct.

The fix is wiring — prefixes on the router constructors — so this asserts the
property rather than the wiring, and would fail again if someone re-introduced
a prefix at include time.
"""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from starlette.responses import Response

PATH_PARAM = re.compile(r"\{[^}]+\}")


@pytest.fixture
def reported_routes(client, monkeypatch):
    """Request every documented route; record what scope["route"].path said.

    `scope["route"]` is set immediately before `APIRoute.handle`, so recording
    there sees exactly what the authorisation layer sees, through the real
    routing. Handlers are not run — the value is decided by then, and running
    them would need a database.
    """
    seen: dict[tuple[str, str], str] = {}

    async def recording_handle(self, scope, receive, send):
        del self
        route = scope.get("route")
        seen[scope["method"], scope["path"]] = getattr(route, "path", None)
        await Response(status_code=204)(scope, receive, send)

    monkeypatch.setattr(APIRoute, "handle", recording_handle)

    documented: list[tuple[str, str, str]] = []
    for template, operations in client.app.openapi()["paths"].items():
        url = PATH_PARAM.sub("x", template)
        for method in operations:
            client.request(method.upper(), url)
            documented.append((method.upper(), url, template))
    return documented, seen


def test_there_are_routes_to_check(reported_routes):
    """A silently empty OpenAPI document would make this file a no-op."""
    documented, _ = reported_routes
    assert len(documented) > 10, f"only {len(documented)} documented operations"


def test_every_route_reports_its_full_path(reported_routes):
    """What PAPI is told must equal what the OpenAPI document promises."""
    documented, seen = reported_routes
    wrong = [
        (method, template, seen.get((method, url)))
        for method, url, template in documented
        if seen.get((method, url)) != template
    ]
    assert not wrong, "\n".join(
        f"{method} {template}: authorisation sees {reported!r}"
        for method, template, reported in wrong
    ) + (
        "\n\nA prefix is being applied at include time or by nesting. Declare it "
        "on the router itself — APIRouter(prefix=...) included with no prefix "
        "argument — so scope['route'].path carries it."
    )
