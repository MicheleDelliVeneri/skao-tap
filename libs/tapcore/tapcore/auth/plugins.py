"""Pluggable authorisation.

Authentication is fixed: every credential is a bearer token whose
authenticity this service verifies against the IAM (``tapcore.auth.tokens``).
What varies between deployments is *authorisation* — the decision, for an
already-verified principal, of whether it may perform an operation.

SRCNet delegates that decision to the SKA SRC Permissions API. A site running
this service outside SRCNet may prefer to decide locally from IAM group
membership and token scopes, or to bring its own policy engine entirely.
So the decision sits behind a plugin, discovered exactly like the metadata
domains: a package registers an :class:`AuthPlugin` under the
``skao_tap.auth`` entry-point group and ``TAP_AUTH_PLUGIN`` selects it.
"""

import logging
from abc import ABC, abstractmethod
from functools import cache
from importlib.metadata import entry_points

from ..config import settings
from .tokens import Principal

log = logging.getLogger("tapcore")

ENTRY_POINT_GROUP = "skao_tap.auth"

# The gated operations. Querying is deliberately absent: TAP clients issue
# queries as POSTs, and gating those would lock standard VO tooling out of an
# authenticated deployment. Only metadata mutation is gated.
OPERATIONS = (
    "metadata.ingest",  # POST   /api/v1/<mount>
    "metadata.amend",  # PATCH  /api/v1/<mount>/{root_id}
    "metadata.delete",  # DELETE /api/v1/<mount>/{root_id}
)


class AuthPlugin(ABC):
    """One deployment's answer to "may this principal do this?".

    Implementations are constructed once at startup and must be safe to call
    from several threads. They never see an unverified token: the framework
    verifies authenticity with the IAM first and passes the resulting
    :class:`~tapcore.auth.tokens.Principal`.
    """

    #: selection key in TAP_AUTH_PLUGIN
    name: str = ""

    @abstractmethod
    def authorize(self, principal: Principal, operation: str, context: dict) -> bool:
        """Return True if ``principal`` may perform ``operation``.

        ``context`` carries request facts a policy may need — ``method``,
        ``route`` (the templated path), ``path_params`` and ``mount``.
        Raising :class:`~tapcore.errors.ServiceError` signals that the
        decision could not be reached (the caller turns that into a 503-style
        failure); returning False is a definite "no".
        """

    def describe(self) -> str:
        """One line for the startup log and the capabilities document."""
        return self.name


@cache
def discovered_auth_plugins() -> dict[str, type[AuthPlugin]]:
    """All authorisation plugins installed in this environment, by name."""
    plugins: dict[str, type[AuthPlugin]] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            plugin = entry.load()
        except Exception:  # a broken third-party plugin must not hide the rest
            log.exception("auth plugin %r failed to load", entry.name)
            continue
        if not (isinstance(plugin, type) and issubclass(plugin, AuthPlugin)):
            log.error("auth plugin %r is not an AuthPlugin subclass", entry.name)
            continue
        plugins[entry.name] = plugin
    return plugins


def active_auth_plugin() -> AuthPlugin | None:
    """The plugin this deployment selected, or None when auth is disabled."""
    if not settings.auth_enabled:
        return None
    available = discovered_auth_plugins()
    name = settings.auth_plugin.strip()
    if name not in available:
        known = ", ".join(sorted(available)) or "none"
        raise LookupError(
            f"TAP_AUTH_PLUGIN selects unknown auth plugin {name!r} (installed: {known})"
        )
    return available[name]()
