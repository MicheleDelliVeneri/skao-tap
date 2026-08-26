"""Authorisation delegated to the SKA SRC Permissions API.

The SRCNet plugin. Policy lives in the Permissions API — IAM groups are bound
to roles and roles to routes — so this service holds no policy of its own; it
asks, per request, whether the token may take this method on this route:

    POST {url}/authorise/route/{service}?route=&method=&version=&token=

with the request's path parameters as the JSON body, answering
``{"is_authorised": bool}``. That is the same contract the Data Management
API uses through the vendored ``ska_src_permissions_api`` client; it is
spoken directly here because those client packages are published to SKA's
internal index rather than PyPI, and one POST is not worth a private index in
every build.

Unlike DMAPI, the token is verified against the IAM's JWKS *before* it is
sent: the Permissions API is asked what a known principal may do, never asked
to vouch for an unknown string.
"""

import logging

import httpx
from egernia_core.auth import AuthPlugin, Principal
from egernia_core.config import settings
from egernia_core.errors import ServiceError

log = logging.getLogger("egernia_api")


class PermissionsApiPlugin(AuthPlugin):
    name = "permissions-api"

    def __init__(self, url: str | None = None, service: str | None = None):
        self.url = (url if url is not None else settings.permissions_api_url).rstrip("/")
        if not self.url:
            raise ServiceError(
                "the permissions-api auth plugin needs TAP_PERMISSIONS_API_URL"
                " (Helm: auth.permissionsApi.url)"
            )
        self.service = service or settings.permissions_service_name
        self.version = settings.permissions_service_version
        self.timeout_s = settings.permissions_timeout_s

    def authorize(self, principal: Principal, operation: str, context: dict) -> bool:
        if principal.is_anonymous or not principal.token:
            return False
        try:
            response = httpx.post(
                f"{self.url}/authorise/route/{self.service}",
                params={
                    "route": context.get("route", ""),
                    "method": context.get("method", ""),
                    "version": self.version,
                    "token": principal.token,
                },
                json=context.get("path_params") or {},
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as exc:
            # fail closed, but as a service fault: a permissions API that is
            # down is an outage, not a denial the caller can act on
            raise ServiceError(f"permissions API at {self.url} is unreachable") from exc
        if response.status_code in (401, 403):
            return False
        if response.status_code >= 400:
            raise ServiceError(
                f"permissions API returned HTTP {response.status_code} for {operation}"
            )
        try:
            decision = response.json()
        except ValueError as exc:
            raise ServiceError("permissions API returned a non-JSON decision") from exc
        return bool(decision.get("is_authorised", False))

    def describe(self) -> str:
        return f"{self.name} ({self.url}, service={self.service} v{self.version})"
