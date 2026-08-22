"""Authentication and authorisation: IAM token verification plus pluggable policy."""

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
    "Principal",
    "active_auth_plugin",
    "discovered_auth_plugins",
    "reset_verifier",
    "verifier",
]
