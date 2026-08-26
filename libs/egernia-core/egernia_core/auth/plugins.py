"""Pluggable authorisation.

Authentication is fixed: every credential is a bearer token whose
authenticity this service verifies against the IAM (``egernia_core.auth.tokens``).
What varies between deployments is *authorisation* — the decision, for an
already-verified principal, of whether it may perform an operation.

SRCNet delegates that decision to the SKA SRC Permissions API. A site running
this service outside SRCNet may prefer to decide locally from IAM group
membership and token scopes, or to bring its own policy engine entirely.
So the decision sits behind a plugin, discovered exactly like the metadata
domains: a package registers an :class:`AuthPlugin` under the
``egernia.auth`` entry-point group and ``TAP_AUTH_PLUGIN`` selects it.
"""

from abc import ABC, abstractmethod
from functools import cache

from .. import load_entry_points
from ..config import settings
from .tokens import Principal

ENTRY_POINT_GROUP = "egernia.auth"

# Every operation the gate knows how to enforce.
OPERATIONS = (
    "metadata.ingest",  # POST   /api/v1/<mount>
    "metadata.amend",  # PATCH  /api/v1/<mount>/{root_id}
    "metadata.delete",  # DELETE /api/v1/<mount>/{root_id}
    "jobs.create",  # POST   /tap/async
    "jobs.mutate",  # POST   /tap/async/{job_id}/(phase|parameters|...)
    "jobs.delete",  # DELETE /tap/async/{job_id}, POST ...?ACTION=DELETE
    "query.sync",  # GET|POST /tap/sync
)

# What is enforced unless the deployment says otherwise: metadata mutation
# only. Querying is absent by default on purpose — TAP clients submit queries
# as POSTs to /sync and /async, so enforcing jobs.create or query.sync turns
# away every standard VO client that carries no token. A site that wants that
# asks for it through TAP_AUTH_GATED_OPERATIONS.
DEFAULT_GATED_OPERATIONS = (
    "metadata.ingest",
    "metadata.amend",
    "metadata.delete",
)

# The query surface, enforced as a group. Each of these reaches the same data
# by a different route: an anonymous caller refused at POST /tap/async runs
# the query at /tap/sync instead, and a deployment that gates job mutation
# without job creation only hands a VO client a job it cannot start. Gating a
# subset is not a weaker policy, it is an incoherent one.
QUERY_OPERATIONS = (
    "jobs.create",
    "jobs.mutate",
    "jobs.delete",
    "query.sync",
)

#: the one value that enforces nothing while authentication stays on
NOTHING_GATED = "none"


def gated_operations() -> tuple[str, ...]:
    """The operations this deployment enforces, in ``OPERATIONS`` order.

    Read per request rather than cached: the setting is part of the same
    configuration a test (or a reloaded process) can change underneath us,
    and parsing a short comma-separated list is not worth a cache to get
    wrong.
    """
    raw = settings.auth_gated_operations.strip()
    if not raw:
        return DEFAULT_GATED_OPERATIONS
    if raw.lower() == NOTHING_GATED:
        # verify tokens and record ownership, enforce nothing. Spelled out
        # rather than inferred from an empty-ish value, because "nothing is
        # gated" is the one answer that must never be reached by accident.
        return ()
    named = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not named:
        # e.g. "," or ", ,": a value was set, so the operator meant
        # something by it, and it was not the default
        raise LookupError(
            f"TAP_AUTH_GATED_OPERATIONS is set to {raw!r} but names no operation;"
            f" leave it empty for the default ({', '.join(DEFAULT_GATED_OPERATIONS)})"
            f" or set it to {NOTHING_GATED!r} to enforce nothing"
        )
    unknown = [name for name in named if name not in OPERATIONS]
    if unknown:
        raise LookupError(
            f"TAP_AUTH_GATED_OPERATIONS names unknown operation(s)"
            f" {', '.join(sorted(unknown))}; known operations:"
            f" {', '.join(OPERATIONS)}"
        )
    partial = [name for name in QUERY_OPERATIONS if name not in named]
    if partial and any(name in named for name in QUERY_OPERATIONS):
        raise LookupError(
            "TAP_AUTH_GATED_OPERATIONS enforces part of the query surface but"
            f" not {', '.join(partial)}. These four are enforced together or"
            " not at all: an anonymous caller refused at POST /tap/async runs"
            " the same query at /tap/sync, and gating job mutation without job"
            " creation hands a VO client a job it cannot start. Add"
            f" {', '.join(partial)}, or drop the query operations entirely."
        )
    # de-duplicated and ordered like OPERATIONS, so the startup log and the
    # capabilities document read the same whatever order an operator typed
    return tuple(name for name in OPERATIONS if name in named)


class AuthPlugin(ABC):
    """One deployment's answer to "may this principal do this?".

    Implementations are constructed once at startup and must be safe to call
    from several threads. They never see an unverified token: the framework
    verifies authenticity with the IAM first and passes the resulting
    :class:`~egernia_core.auth.tokens.Principal`.
    """

    #: selection key in TAP_AUTH_PLUGIN
    name: str = ""

    @abstractmethod
    def authorize(self, principal: Principal, operation: str, context: dict) -> bool:
        """Return True if ``principal`` may perform ``operation``.

        ``context`` carries request facts a policy may need — ``method``,
        ``route`` (the templated path), ``path_params`` and ``mount``.
        Raising :class:`~egernia_core.errors.ServiceError` signals that the
        decision could not be reached (the caller turns that into a 503-style
        failure); returning False is a definite "no".
        """

    def describe(self) -> str:
        """One line for the startup log and the capabilities document."""
        return self.name


@cache
def discovered_auth_plugins() -> dict[str, type[AuthPlugin]]:
    """All authorisation plugins installed in this environment, by name."""
    return dict(
        load_entry_points(
            ENTRY_POINT_GROUP,
            lambda obj: isinstance(obj, type) and issubclass(obj, AuthPlugin),
            "auth",
        )
    )


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
