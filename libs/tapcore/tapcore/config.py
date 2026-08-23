"""Environment-driven configuration shared by all services."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "TAP_DATABASE_URL", "postgresql://tap:tap@localhost:5432/tap"
        )
    )
    base_url: str = field(
        default_factory=lambda: os.getenv("TAP_BASE_URL", "http://localhost:8080/tap")
    )
    results_dir: str = field(default_factory=lambda: os.getenv("TAP_RESULTS_DIR", "/results"))
    query_role: str = field(default_factory=lambda: os.getenv("TAP_QUERY_ROLE", "tap_reader"))
    default_maxrec: int = field(
        default_factory=lambda: int(os.getenv("TAP_DEFAULT_MAXREC", "10000"))
    )
    hard_maxrec: int = field(default_factory=lambda: int(os.getenv("TAP_HARD_MAXREC", "1000000")))
    sync_timeout_s: int = field(default_factory=lambda: int(os.getenv("TAP_SYNC_TIMEOUT", "30")))
    default_exec_duration_s: int = field(
        default_factory=lambda: int(os.getenv("TAP_ASYNC_EXEC_DURATION", "600"))
    )
    job_retention_s: int = field(
        default_factory=lambda: int(os.getenv("TAP_JOB_RETENTION", str(7 * 24 * 3600)))
    )
    upload_max_rows: int = field(
        default_factory=lambda: int(os.getenv("TAP_UPLOAD_MAX_ROWS", "100000"))
    )
    upload_max_bytes: int = field(
        default_factory=lambda: int(os.getenv("TAP_UPLOAD_MAX_BYTES", str(32 * 1024 * 1024)))
    )
    wait_max_s: int = field(default_factory=lambda: int(os.getenv("TAP_WAIT_MAX", "60")))
    model_plugins: str = field(default_factory=lambda: os.getenv("TAP_MODEL_PLUGINS", "all"))
    log_level: str = field(default_factory=lambda: os.getenv("TAP_LOG_LEVEL", "INFO"))

    # -- authentication and authorisation ----------------------------------
    # Off by default: a deployment without an IAM keeps working exactly as it
    # did, which is what local development and the demo notebook rely on.
    auth_enabled: bool = field(
        default_factory=lambda: (
            os.getenv("TAP_AUTH_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
        )
    )
    auth_plugin: str = field(default_factory=lambda: os.getenv("TAP_AUTH_PLUGIN", "iam-groups"))
    # With authentication on, every endpoint except service discovery and the
    # health check needs a verified token — reads included. Authorisation
    # (which token may do what) stays with auth_gated_operations below.
    auth_require_token: bool = field(
        default_factory=lambda: (
            os.getenv("TAP_AUTH_REQUIRE_TOKEN", "true").strip().lower()
            in ("1", "true", "yes", "on")
        )
    )
    # per-operation policy for the iam-groups plugin, as JSON:
    # {"metadata.ingest": {"groups": [...], "scopes": [...]}, ...}
    auth_roles: str = field(default_factory=lambda: os.getenv("TAP_AUTH_ROLES", "{}"))
    # which operations the gate actually enforces, comma-separated; empty
    # means the default set (metadata mutation only). Adding jobs.create or
    # query.sync makes a deployment reject anonymous querying, which locks
    # out VO clients that send no token — so it has to be asked for.
    auth_gated_operations: str = field(
        default_factory=lambda: os.getenv("TAP_AUTH_GATED_OPERATIONS", "")
    )
    # token authenticity is always verified against this issuer, whatever
    # plugin decides authorisation
    iam_issuer: str = field(default_factory=lambda: os.getenv("TAP_IAM_ISSUER", ""))
    iam_audience: str = field(default_factory=lambda: os.getenv("TAP_IAM_AUDIENCE", ""))
    # accepting any audience lets tokens minted for other clients of the same
    # IAM be replayed here, so it must be asked for explicitly
    iam_allow_any_audience: bool = field(
        default_factory=lambda: (
            os.getenv("TAP_IAM_ALLOW_ANY_AUDIENCE", "false").strip().lower()
            in ("1", "true", "yes", "on")
        )
    )
    iam_group_claims: str = field(
        default_factory=lambda: os.getenv("TAP_IAM_GROUP_CLAIMS", "groups,wlcg.groups")
    )
    iam_well_known_url: str = field(default_factory=lambda: os.getenv("TAP_IAM_WELL_KNOWN_URL", ""))
    iam_jwks_cache_s: int = field(
        default_factory=lambda: int(os.getenv("TAP_IAM_JWKS_CACHE", "300"))
    )
    # SKA SRC Permissions API, for the permissions-api plugin
    permissions_api_url: str = field(
        default_factory=lambda: os.getenv("TAP_PERMISSIONS_API_URL", "")
    )
    permissions_service_name: str = field(
        default_factory=lambda: os.getenv("TAP_PERMISSIONS_SERVICE_NAME", "science-metadata")
    )
    permissions_service_version: str = field(
        default_factory=lambda: os.getenv("TAP_PERMISSIONS_SERVICE_VERSION", "1")
    )
    permissions_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("TAP_PERMISSIONS_TIMEOUT", "10"))
    )


settings = Settings()
