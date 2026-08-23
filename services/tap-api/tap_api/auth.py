"""Request-level authentication and authorisation wiring.

One dependency factory, ``require(operation)``, guards every gated
operation: the mutating metadata endpoints, the UWS job resources that create,
mutate or delete a job, and synchronous querying.

Which of those a deployment actually enforces is its own choice
(``TAP_AUTH_GATED_OPERATIONS``), because the choice is not free: TAP clients
submit queries as POSTs, so enforcing ``jobs.create`` or ``query.sync`` turns
away every standard VO client that carries no token. The default enforces
metadata mutation only, which is what a deployment upgrading into this had.

An operation this deployment does not enforce is let through exactly as it
would be with auth off — but a token presented on any request is still
verified and attached to ``request.state.principal``, so ownership and audit
records get a subject when one is available.

Authentication and authorisation are separate questions here, and the answers
have different defaults. With ``TAP_AUTH_REQUIRE_TOKEN`` (the default), a
request needs a *verified token*, reads included; the gate set then decides
which requests additionally need a *decision* about that token. So a metadata
`GET` needs a token and nothing more, while deleting a metadata document
needs a token the plugin (IAM groups, or the SRCNet Permissions API)
approves.

One set of paths stays open regardless (``ANONYMOUS_PATHS``): the endpoints a
client reaches *before* it has a token, because the challenge it gets back is
what names the IAM, plus the health check a probe asks for. Reading metadata
through TAP — ``/tap/sync`` and the ``/tap/async`` job — can be opened on top
of that with ``TAP_AUTH_ANONYMOUS_QUERIES``, which is what a deployment that
wants to serve standard VO clients sets.
"""

import logging

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from tapcore.auth import (
    ANONYMOUS,
    QUERY_OPERATIONS,
    Principal,
    active_auth_plugin,
    clear_job_viewer,
    gated_operations,
    missing_token_challenge,
    set_job_viewer,
    verifier,
)
from tapcore.auth.challenge import discovery_url
from tapcore.config import settings
from tapcore.errors import AuthenticationError, AuthorizationError

log = logging.getLogger("tap_api")

# None is a legitimate resolved value (auth disabled), so "not resolved yet"
# needs a sentinel of its own rather than a second flag
_UNRESOLVED = object()
_PLUGIN: object = _UNRESOLVED
_GATED: tuple[str, ...] | None = None


def plugin():
    """The active authorisation plugin, resolved once."""
    global _PLUGIN
    if _PLUGIN is _UNRESOLVED:
        resolved = active_auth_plugin()
        if resolved is None:
            log.warning(
                "authentication is DISABLED: every endpoint, including metadata"
                " ingest, amendment and deletion, is open to anonymous callers"
            )
        else:
            log.info("authorisation plugin: %s", resolved.describe())
            log.info("enforced operations: %s", ", ".join(gated()))
        # assigned only after a successful resolve, so a misconfiguration
        # keeps raising instead of being cached as "auth off"
        _PLUGIN = resolved
    return _PLUGIN


def gated() -> tuple[str, ...]:
    """The operations this deployment enforces, resolved once.

    Resolved here rather than per request so a malformed
    ``TAP_AUTH_GATED_OPERATIONS`` is reported the same way a bad plugin name
    is, instead of once per call.
    """
    global _GATED
    if _GATED is None:
        _GATED = gated_operations()
    return _GATED


def reset_plugin() -> None:
    """Forget the resolved plugin and gate set (configuration changed; tests)."""
    global _PLUGIN, _GATED
    _PLUGIN = _UNRESOLVED
    _GATED = None


# Reachable without a token even when one is required everywhere else.
#
# Two kinds of request are here. Service discovery and the health check are
# asked by something that cannot hold a credential: a Kubernetes probe
# (availability), a registry harvester or a VO client browsing for services
# (capabilities, tables, the VOResource record, examples), and a client that
# has not authenticated yet and is working out how to (/api/v1/auth, the
# OpenAPI documents). Gating those would mean a service that cannot be
# monitored, registered or discovered.
ANONYMOUS_PATHS = frozenset(
    {
        "/",
        # The probe endpoints. A kubelet has no token and no way to obtain one,
        # so gating these would fail every probe the moment auth is enabled and
        # Kubernetes would then kill every pod — an outage caused by turning on
        # authentication.
        "/health/live",
        "/health/ready",
        "/tap/availability",
        "/tap/capabilities",
        "/tap/tables",
        "/tap/registry",
        "/tap/examples",
        "/api/v1/auth",
        # FastAPI registers these three itself, outside the app-level
        # dependency, so they are exempt by construction as well as by this
        # list — which is kept so the intent is written down rather than
        # inferred from a framework detail
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)

# Reading metadata through TAP itself — a synchronous query and the UWS job
# that runs one — is the second kind, and unlike the paths above it is a
# deployment's choice (``auth_anonymous_queries``). Standard VO clients cannot
# authenticate, so opening these is what decides whether PyVO and TOPCAT can
# use the service at all; closed by default, because "who may read this
# archive" is not a question to answer by inheriting a default.
#
# Prefixes, because a job is a tree of sub-resources (/phase, /parameters,
# /results, …) and every one of them belongs to the same read.
QUERY_PREFIXES = ("/tap/sync", "/tap/async")


def _is_capability_discovery(method: str, path: str, query_params) -> bool:
    """A TAP 1.0 client discovering capabilities through /sync.

    ``GET /tap/sync?REQUEST=getCapabilities`` is not a query: the handler
    redirects it to /capabilities, which is open. Demanding a token for the
    redirect while its destination is public would break capability discovery
    for older clients, and tell them nothing they could act on.

    The exemption must never be wider than the redirect it exists for, or it
    becomes a way past the token requirement — a request the handler runs as a
    query would have been let through as discovery. Two things keep it
    narrower:

    * ``GET`` only. ``gather_params`` merges the POST form *over* the query
      string, so on a POST the query string does not decide what the handler
      does: ``POST /tap/sync?REQUEST=getCapabilities`` with a form body of
      ``REQUEST=doQuery&QUERY=...`` runs the query. Reading the body here to
      find that out would mean consuming it before the handler can.
    * The same comparison the handler makes — ``gather_params`` upper-cases
      the key and keeps the last value, and ``sync()`` compares that value to
      ``"getCapabilities"`` as-is.
    """
    if method.upper() != "GET" or path.rstrip("/") != "/tap/sync":
        return False
    if query_params is None:
        return False
    request_value = None
    for key, value in query_params.multi_items():
        if key.upper() == "REQUEST":
            request_value = value
    return request_value == "getCapabilities"


def needs_token(path: str, query_params=None, method: str = "GET") -> bool:
    """Whether a request must carry a verified token.

    ``query_params`` and ``method`` are needed only to recognise capability
    discovery through /sync.
    """
    if not settings.auth_require_token:
        return False
    trimmed = path.rstrip("/") or "/"
    if trimmed in ANONYMOUS_PATHS or path in ANONYMOUS_PATHS:
        return False
    if _is_capability_discovery(method, path, query_params):
        return False
    return not (
        settings.auth_anonymous_queries
        and any(trimmed == prefix or trimmed.startswith(f"{prefix}/") for prefix in QUERY_PREFIXES)
    )


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        # no usable credential was presented, so this is the discovery case:
        # invalid_token would tell the client to refresh a token it never had
        raise AuthenticationError(
            "Authorization header must be 'Bearer <token>'",
            challenge=missing_token_challenge("Malformed Authorization header"),
        )
    return credential.strip()


def principal_of(request: Request) -> Principal:
    """Verify any token on the request and cache the principal on it.

    A malformed or unverifiable token is always an error, even where no token
    was required: silently treating it as anonymous would hide a broken client
    or an expired credential behind a working-looking request.
    """
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    token = _bearer(request)
    resolved = ANONYMOUS if token is None else verifier().verify(token)
    request.state.principal = resolved
    return resolved


async def attach_principal(request: Request) -> None:
    """App-wide dependency: verify any credential the request carries, and
    put the resulting identity in scope for the request.

    Registered on every route, not just the gated ones. An endpoint that
    needs no token still must not accept a forged or expired one — silently
    treating an unverifiable credential as "anonymous" would let a broken or
    tampered-with client look like it is working.

    Async, with the verification itself pushed to a threadpool: fetching the
    IAM's discovery document or JWKS must not block the event loop, but the
    job viewer has to be set in the request's own task, because a context
    variable set inside a threadpool would not propagate back out of it.
    """
    # Set unconditionally, both branches. A context variable is only
    # guaranteed to be isolated per task, and not every ASGI server (nor the
    # test client) gives each request a fresh one — leaving it untouched
    # could let one request be judged against the previous request's
    # identity, which is the worst possible failure for this variable.
    if plugin() is None:
        clear_job_viewer()  # no ownership enforcement: behave as before
        return
    who = await run_in_threadpool(principal_of, request)
    if who.is_anonymous and needs_token(request.url.path, request.query_params, request.method):
        # the challenge names the IAM, so a client that arrived with nothing
        # can go and get a token rather than guess which issuer to ask
        raise AuthenticationError(
            "this deployment requires a bearer token", challenge=missing_token_challenge()
        )
    set_job_viewer(who.subject)


def require(operation: str):
    """A FastAPI dependency gating one operation behind the active plugin.

    A plugin may call out over the network (the permissions-api one does),
    so the decision runs in a threadpool rather than on the event loop.
    """

    async def dependency(request: Request) -> Principal:
        active = plugin()
        if active is None:  # auth disabled: behave exactly as before
            return ANONYMOUS
        if _is_capability_discovery(request.method, request.url.path, request.query_params):
            # same reasoning as the token requirement: this is discovery, not
            # a query, and its destination is public either way
            return getattr(request.state, "principal", ANONYMOUS)
        if operation not in gated():
            # this deployment does not enforce this operation, so the request
            # passes as it would with auth off. attach_principal has already
            # verified any token it carries, so an owner is still recorded.
            return getattr(request.state, "principal", ANONYMOUS)
        who = await run_in_threadpool(principal_of, request)
        if who.is_anonymous:
            raise AuthenticationError(
                f"{operation} requires a bearer token", challenge=missing_token_challenge()
            )
        context = {
            "operation": operation,
            "method": request.method,
            "route": request.scope["route"].path,
            "path_params": dict(request.path_params),
        }
        allowed = await run_in_threadpool(active.authorize, who, operation, context)
        if not allowed:
            log.info("denied %s to subject %s", operation, who.subject)
            raise AuthorizationError(f"not permitted to perform {operation}")
        return who

    dependency.__name__ = f"require_{operation.replace('.', '_')}"
    return dependency


def owner_of(request: Request) -> str | None:
    """The subject to record as a new job's owner, or None when anonymous.

    Jobs created without a token stay ownerless, so they behave as they
    always have; only a job created by an identified caller becomes private
    to that caller.
    """
    if plugin() is None:
        return None
    who = getattr(request.state, "principal", None)
    return who.subject if who is not None else None


def anonymous_tap_queries() -> bool:
    """Whether a caller with no token can read metadata through TAP.

    Derived, because three settings decide it and a client should not have to
    combine them: the token requirement can be off entirely, or on with
    ``anonymous_queries`` reopening the TAP paths — and either way the gate can
    still refuse a token-less caller if the query operations are enforced.
    """
    if plugin() is None:
        return True
    reachable = not settings.auth_require_token or settings.auth_anonymous_queries
    return reachable and not any(name in gated() for name in QUERY_OPERATIONS)


def auth_summary() -> dict:
    """What this deployment enforces, for the service's own metadata."""
    active = plugin()
    if active is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "plugin": active.name,
        "token_required": settings.auth_require_token,
        "anonymous_queries": settings.auth_anonymous_queries,
        # the answer to "can I query without a token?", so a client does not
        # have to work it out from the three fields above
        "anonymous_tap_queries": anonymous_tap_queries(),
        "discovery_url": discovery_url(),
        "issuer": settings.iam_issuer,
        "audience": settings.iam_audience or None,
    }
