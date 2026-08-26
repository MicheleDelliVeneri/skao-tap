"""Authentication and authorisation: IAM token verification plus pluggable policy."""

from .challenge import invalid_token_challenge, missing_token_challenge
from .context import JobViewer, clear_job_viewer, current_job_viewer, set_job_viewer
from .plugins import (
    OPERATIONS,
    QUERY_OPERATIONS,
    AuthPlugin,
    active_auth_plugin,
    discovered_auth_plugins,
    gated_operations,
)
from .tokens import (
    ANONYMOUS,
    IAMTokenVerifier,
    Principal,
    reset_verifier,
    verifier,
)

__all__ = [
    "ANONYMOUS",
    "OPERATIONS",
    "QUERY_OPERATIONS",
    "AuthPlugin",
    "IAMTokenVerifier",
    "JobViewer",
    "Principal",
    "active_auth_plugin",
    "clear_job_viewer",
    "current_job_viewer",
    "discovered_auth_plugins",
    "gated_operations",
    "invalid_token_challenge",
    "missing_token_challenge",
    "reset_verifier",
    "set_job_viewer",
    "verifier",
]
