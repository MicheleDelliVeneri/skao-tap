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

Every gated operation must be granted explicitly: an operation missing from
the mapping, or present with neither groups nor scopes, is denied. Accepting
any verified token is a deliberate choice that has to be written down:

    "metadata.amend": {"any_verified_token": true}

Silently allowing an operation nobody configured is the wrong default for a
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
        groups, scopes, any_verified_token = rule
        if any_verified_token:
            return True
        if not groups and not scopes:
            # an operation configured with nothing grants nothing: the empty
            # rule is what an unfinished policy looks like, not a decision
            log.warning(
                "TAP_AUTH_ROLES[%s] names no group or scope and does not set"
                " any_verified_token: denying",
                operation,
            )
            return False
        return bool(set(groups) & set(principal.groups)) or bool(
            set(scopes) & set(principal.scopes)
        )

    def describe(self) -> str:
        # spell out what each operation actually grants: "configured" alone
        # would read as reassuring for a rule that grants nothing, or for one
        # that lets any verified token through
        if not self.roles:
            return f"{self.name} (no operation is granted to anyone)"
        parts = []
        for operation, (groups, scopes, any_verified_token) in sorted(self.roles.items()):
            if any_verified_token:
                grant = "ANY verified token"
            elif groups or scopes:
                grant = " or ".join([*groups, *(f"scope:{s}" for s in scopes)])
            else:
                grant = "nobody (no group or scope configured)"
            parts.append(f"{operation}={grant}")
        return f"{self.name} ({'; '.join(parts)})"


def _parse_roles(raw) -> dict[str, tuple[tuple[str, ...], tuple[str, ...], bool]]:
    """Normalize the JSON policy into {operation: (groups, scopes, any_token)}."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError as exc:
            raise ServiceError("TAP_AUTH_ROLES is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ServiceError("TAP_AUTH_ROLES must be a JSON object keyed by operation")

    def _sequence(value, operation, key):
        """A bare string here would iterate into characters and silently
        produce a policy of one-letter group names that grants nothing."""
        if value is None:
            return ()
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ServiceError(
                f"TAP_AUTH_ROLES[{operation!r}][{key!r}] must be a list, got {type(value).__name__}"
            )
        return value

    parsed = {}
    for operation, rule in raw.items():
        if not isinstance(rule, dict):
            raise ServiceError(f"TAP_AUTH_ROLES[{operation!r}] must be an object")
        raw_groups = _sequence(rule.get("groups"), operation, "groups")
        raw_scopes = _sequence(rule.get("scopes"), operation, "scopes")
        groups = tuple(f"/{str(g).lstrip('/')}" for g in raw_groups)
        scopes = tuple(str(s) for s in raw_scopes)
        parsed[operation] = (groups, scopes, bool(rule.get("any_verified_token", False)))
    return parsed
