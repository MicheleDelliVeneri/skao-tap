"""Request-level authentication and authorisation wiring.

One dependency factory, ``require(operation)``, guards the mutating metadata
endpoints. Reads and queries stay open — TAP clients issue queries as POSTs,
so gating them would lock standard VO tooling out of an authenticated
deployment — but a token presented on any request is still verified and
attached to ``request.state.principal``, so ownership and audit records get a
subject when one is available.
"""

import logging

from fastapi import Request
from tapcore.auth import ANONYMOUS, Principal, active_auth_plugin, verifier
from tapcore.config import settings
from tapcore.errors import AuthenticationError, AuthorizationError

log = logging.getLogger("tap_api")

_PLUGIN = None
_PLUGIN_LOADED = False


def plugin():
    """The active authorisation plugin, resolved once."""
    global _PLUGIN, _PLUGIN_LOADED
    if not _PLUGIN_LOADED:
        _PLUGIN = active_auth_plugin()
        _PLUGIN_LOADED = True
        if _PLUGIN is None:
            log.warning(
                "authentication is DISABLED: every endpoint, including metadata"
                " ingest, amendment and deletion, is open to anonymous callers"
            )
        else:
            log.info("authorisation plugin: %s", _PLUGIN.describe())
    return _PLUGIN


def reset_plugin() -> None:
    """Forget the resolved plugin (configuration changed; used by tests)."""
    global _PLUGIN, _PLUGIN_LOADED
    _PLUGIN, _PLUGIN_LOADED = None, False


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        raise AuthenticationError("Authorization header must be 'Bearer <token>'")
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
    """App-wide dependency: verify any credential the request carries.

    Registered on every route, not just the gated ones. An endpoint that
    needs no token still must not accept a forged or expired one — silently
    treating an unverifiable credential as "anonymous" would let a broken or
    tampered-with client look like it is working.
    """
    if plugin() is None:
        return
    principal_of(request)


def require(operation: str):
    """A FastAPI dependency gating one operation behind the active plugin."""

    async def dependency(request: Request) -> Principal:
        active = plugin()
        if active is None:  # auth disabled: behave exactly as before
            return ANONYMOUS
        who = principal_of(request)
        if who.is_anonymous:
            raise AuthenticationError(f"{operation} requires a bearer token")
        context = {
            "operation": operation,
            "method": request.method,
            "route": request.scope["route"].path,
            "path_params": dict(request.path_params),
        }
        if not active.authorize(who, operation, context):
            log.info("denied %s to subject %s", operation, who.subject)
            raise AuthorizationError(f"not permitted to perform {operation}")
        return who

    dependency.__name__ = f"require_{operation.replace('.', '_')}"
    return dependency


def auth_summary() -> dict:
    """What this deployment enforces, for the service's own metadata."""
    active = plugin()
    if active is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "plugin": active.name,
        "issuer": settings.iam_issuer,
        "audience": settings.iam_audience or None,
    }
