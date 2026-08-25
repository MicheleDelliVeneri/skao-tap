"""The identity a request is acting under, as ambient request state.

Job ownership has to hold on every path that can reach a job — the UWS
resources, their sub-resources, the JSON facade, the result download — which
is around twenty call sites today and grows whenever a resource is added.
Threading an owner argument through all of them puts the burden in the wrong
place: the one that gets forgotten is a leak.

So the viewer is set once per request and enforced in the job store, where
every path already funnels through. The default is *unrestricted*, which is
what the executor (no request, must see every job) and every deployment with
authentication disabled need.
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class JobViewer:
    """Who is looking at the job store, when ownership is being enforced."""

    subject: str | None  # None: an authenticated deployment, anonymous caller

    def may_see(self, owner_id: str | None) -> bool:
        # A job created anonymously has no owner to protect, so it stays
        # visible to everyone, exactly as it was before ownership existed.
        # A job with an owner belongs to that subject alone.
        return owner_id is None or owner_id == self.subject


#: unset means "no ownership enforcement" — the executor and unauthenticated
#: deployments read and write the whole job store
_VIEWER: ContextVar[JobViewer | None] = ContextVar("egernia_core_job_viewer", default=None)


def set_job_viewer(subject: str | None) -> None:
    """Enforce ownership for the rest of this request, as ``subject``."""
    _VIEWER.set(JobViewer(subject=subject))


def current_job_viewer() -> JobViewer | None:
    """The viewer to enforce against, or None when ownership is not enforced."""
    return _VIEWER.get()


def clear_job_viewer() -> None:
    """Stop enforcing ownership (used by tests)."""
    _VIEWER.set(None)
