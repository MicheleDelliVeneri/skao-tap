"""Authentication and authorisation: IAM token verification plus pluggable policy."""

from .context import JobViewer, clear_job_viewer, current_job_viewer, set_job_viewer
from .plugins import OPERATIONS, AuthPlugin, active_auth_plugin, discovered_auth_plugins
from .tokens import (
    ANONYMOUS,
    DEFAULT_GROUP_CLAIMS,
    IAMTokenVerifier,
    Principal,
    reset_verifier,
    verifier,
)

__all__ = [
    "ANONYMOUS",
    "DEFAULT_GROUP_CLAIMS",
    "OPERATIONS",
    "AuthPlugin",
    "IAMTokenVerifier",
    "JobViewer",
    "Principal",
    "active_auth_plugin",
    "clear_job_viewer",
    "current_job_viewer",
    "discovered_auth_plugins",
    "reset_verifier",
    "set_job_viewer",
    "verifier",
]
