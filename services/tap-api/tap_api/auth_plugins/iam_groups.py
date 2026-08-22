"""Local authorisation from IAM group membership and token scopes.

The default plugin, for deployments that authenticate against an IAM but do
not run the SRCNet Permissions API. Each gated operation names the groups and
the scopes that grant it; a principal is allowed when its verified token
carries any one of them:

    TAP_AUTH_ROLES='{
      "metadata.ingest": {"groups": ["/ska/science-metadata/oper"]},
      "metadata.amend":  {"groups": ["/ska/science-metadata/oper"]},
      "metadata.delete": {"groups": ["/ska/science-metadata/oper"],
                          "scopes": ["science-metadata:admin"]}
    }'

An operation with no groups and no scopes requires a verified token and
nothing more. An operation missing from the mapping is denied outright:
silently allowing an operation nobody configured is the wrong default for a
policy file, especially for deletion.
"""

import json
import logging

from tapcore.auth import AuthPlugin, Principal
from tapcore.auth.plugins import OPERATIONS
from tapcore.config import settings
from tapcore.errors import ServiceError

log = logging.getLogger("tap_api")


class IAMGroupsPlugin(AuthPlugin):
    name = "iam-groups"

    def __init__(self, roles: dict | None = None):
        self.roles = _parse_roles(settings.auth_roles if roles is None else roles)
        unknown = set(self.roles) - set(OPERATIONS)
        if unknown:
            raise ServiceError(
                f"TAP_AUTH_ROLES names unknown operation(s) {', '.join(sorted(unknown))};"
                f" known operations: {', '.join(OPERATIONS)}"
            )

    def authorize(self, principal: Principal, operation: str, context: dict) -> bool:
        if principal.is_anonymous:
            return False
        rule = self.roles.get(operation)
        if rule is None:
            log.warning("no TAP_AUTH_ROLES entry for %s: denying", operation)
            return False
        groups, scopes = rule
        if not groups and not scopes:
            return True  # any verified token is enough for this operation
        return bool(set(groups) & set(principal.groups)) or bool(
            set(scopes) & set(principal.scopes)
        )

    def describe(self) -> str:
        configured = ", ".join(sorted(self.roles)) or "nothing"
        return f"{self.name} (operations configured: {configured})"


def _parse_roles(raw) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """Normalize the JSON policy into {operation: (groups, scopes)}."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError as exc:
            raise ServiceError("TAP_AUTH_ROLES is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ServiceError("TAP_AUTH_ROLES must be a JSON object keyed by operation")
    parsed = {}
    for operation, rule in raw.items():
        if not isinstance(rule, dict):
            raise ServiceError(f"TAP_AUTH_ROLES[{operation!r}] must be an object")
        groups = tuple(f"/{str(g).lstrip('/')}" for g in rule.get("groups") or ())
        scopes = tuple(str(s) for s in rule.get("scopes") or ())
        parsed[operation] = (groups, scopes)
    return parsed
