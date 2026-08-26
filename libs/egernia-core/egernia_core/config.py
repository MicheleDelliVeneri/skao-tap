"""Environment-driven configuration shared by all services."""

import os
from dataclasses import dataclass, field

TRUE_VALUES = ("1", "true", "yes", "on")
FALSE_VALUES = ("0", "false", "no", "off")


def _flag(name: str, default: bool) -> bool:
    """Read a boolean environment variable, refusing anything ambiguous.

    A typo must not decide a security question quietly. Mapping every
    unrecognised value to False is how ``TAP_AUTH_REQUIRE_TOKEN=flase`` turns
    the token requirement off without saying so, so an unrecognised value is
    an error and the service refuses to start. Unset, or set to nothing at
    all, means the default.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ValueError(
        f"{name}={raw!r} is not a boolean; use one of"
        f" {', '.join(TRUE_VALUES)} or {', '.join(FALSE_VALUES)}"
    )


# Field helpers: each setting reads its environment variable at Settings()
# construction time (default_factory), not at import time.
def _env(name: str, default: str = ""):
    return field(default_factory=lambda: os.getenv(name, default))


def _int(name: str, default: str):
    return field(default_factory=lambda: int(os.getenv(name, default)))


def _float(name: str, default: str):
    return field(default_factory=lambda: float(os.getenv(name, default)))


def _bool(name: str, default: bool):
    return field(default_factory=lambda: _flag(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str = _env("TAP_DATABASE_URL", "postgresql://tap:tap@localhost:5432/tap")
    base_url: str = _env("TAP_BASE_URL", "http://localhost:8080/tap")
    results_dir: str = _env("TAP_RESULTS_DIR", "/results")
    # How long the readiness probe waits for a connection before answering
    # "busy". Short on purpose and separate from db_pool_timeout_s: a probe
    # that waits as long as a user query reports on the queue depth rather
    # than on whether the database is reachable.
    health_probe_timeout_s: float = _float("TAP_HEALTH_PROBE_TIMEOUT", "1.0")
    query_role: str = _env("TAP_QUERY_ROLE", "tap_reader")
    default_maxrec: int = _int("TAP_DEFAULT_MAXREC", "10000")
    hard_maxrec: int = _int("TAP_HARD_MAXREC", "1000000")
    sync_timeout_s: int = _int("TAP_SYNC_TIMEOUT", "30")
    default_exec_duration_s: int = _int("TAP_ASYNC_EXEC_DURATION", "600")
    job_retention_s: int = _int("TAP_JOB_RETENTION", str(7 * 24 * 3600))
    upload_max_rows: int = _int("TAP_UPLOAD_MAX_ROWS", "100000")
    upload_max_bytes: int = _int("TAP_UPLOAD_MAX_BYTES", str(32 * 1024 * 1024))
    wait_max_s: int = _int("TAP_WAIT_MAX", "60")
    model_plugins: str = _env("TAP_MODEL_PLUGINS", "all")
    # Prefix of every obs_publisher_did the ivoa.obscore view constructs. A
    # PublisherDID is a permanent promise: in a real deployment the authority
    # must match the registry's authorityId, so this is configuration, not a
    # constant.
    obscore_did_prefix: str = _env("TAP_OBSCORE_DID_PREFIX", "ivo://skao.int/~?")
    log_level: str = _env("TAP_LOG_LEVEL", "INFO")
    # Where to send OpenTelemetry traces. Empty means nowhere, and nothing is
    # instrumented — a deployment without a collector should not pay for one.
    otlp_endpoint: str = field(
        default_factory=lambda: os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    )
    # Port the executor serves its metrics on; the API serves them on its own
    # port at /metrics, but a worker loop has no listener of its own.
    executor_metrics_port: int = _int("TAP_EXECUTOR_METRICS_PORT", "9100")

    # -- database connection pool ------------------------------------------
    # The pool bounds how many queries a process can have in flight, so it is
    # the service's real concurrency limit. Waiting for a connection is
    # bounded too: a request that cannot get one should be told so, not left
    # hanging while the client, and anything proxying for it, stack up behind.
    db_pool_min: int = _int("TAP_DB_POOL_MIN", "1")
    db_pool_max: int = _int("TAP_DB_POOL_MAX", "8")
    db_pool_timeout_s: float = _float("TAP_DB_POOL_TIMEOUT", "5")

    # -- authentication and authorisation ----------------------------------
    # Off by default: a deployment without an IAM keeps working exactly as it
    # did, which is what local development and the demo notebook rely on.
    auth_enabled: bool = _bool("TAP_AUTH_ENABLED", False)
    auth_plugin: str = _env("TAP_AUTH_PLUGIN", "iam-groups")
    # With authentication on, every endpoint except service discovery and the
    # health check needs a verified token — reads included. Authorisation
    # (which token may do what) stays with auth_gated_operations below.
    auth_require_token: bool = _bool("TAP_AUTH_REQUIRE_TOKEN", True)
    # Reopen reading metadata through TAP — /tap/sync and the /tap/async job —
    # to callers with no token. Off by default: standard VO clients cannot
    # authenticate, so this is the switch that decides whether a deployment
    # serves them at all, and that is a decision to take rather than inherit.
    auth_anonymous_queries: bool = _bool("TAP_AUTH_ANONYMOUS_QUERIES", False)
    # per-operation policy for the iam-groups plugin, as JSON:
    # {"metadata.ingest": {"groups": [...], "scopes": [...]}, ...}
    auth_roles: str = _env("TAP_AUTH_ROLES", "{}")
    # which operations the gate actually enforces, comma-separated; empty
    # means the default set (metadata mutation only). Adding jobs.create or
    # query.sync makes a deployment reject anonymous querying, which locks
    # out VO clients that send no token — so it has to be asked for.
    auth_gated_operations: str = _env("TAP_AUTH_GATED_OPERATIONS", "")
    # token authenticity is always verified against this issuer, whatever
    # plugin decides authorisation
    iam_issuer: str = _env("TAP_IAM_ISSUER", "")
    iam_audience: str = _env("TAP_IAM_AUDIENCE", "")
    # accepting any audience lets tokens minted for other clients of the same
    # IAM be replayed here, so it must be asked for explicitly
    iam_allow_any_audience: bool = _bool("TAP_IAM_ALLOW_ANY_AUDIENCE", False)
    iam_group_claims: str = _env("TAP_IAM_GROUP_CLAIMS", "groups,wlcg.groups")
    iam_well_known_url: str = _env("TAP_IAM_WELL_KNOWN_URL", "")
    iam_jwks_cache_s: int = _int("TAP_IAM_JWKS_CACHE", "300")
    # -- VO Registry publication -------------------------------------------
    # The VOResource record served at /tap/registry. Off until a deployment
    # has an IVOA authority to publish under: an identifier is a promise that
    # this URI resolves to this service forever, so it cannot be defaulted.
    registry_enabled: bool = _bool("TAP_REGISTRY_ENABLED", False)
    registry_identifier: str = _env("TAP_REGISTRY_IDENTIFIER", "")
    registry_title: str = _env("TAP_REGISTRY_TITLE", "")
    # VOResource caps shortName at 16 characters
    registry_short_name: str = _env("TAP_REGISTRY_SHORT_NAME", "")
    registry_description: str = _env("TAP_REGISTRY_DESCRIPTION", "")
    registry_reference_url: str = _env("TAP_REGISTRY_REFERENCE_URL", "")
    registry_publisher: str = _env("TAP_REGISTRY_PUBLISHER", "")
    registry_creator: str = _env("TAP_REGISTRY_CREATOR", "")
    registry_contact_name: str = _env("TAP_REGISTRY_CONTACT_NAME", "")
    registry_contact_email: str = _env("TAP_REGISTRY_CONTACT_EMAIL", "")
    # comma-separated; content requires at least one subject
    registry_subjects: str = _env("TAP_REGISTRY_SUBJECTS", "")
    registry_content_levels: str = _env("TAP_REGISTRY_CONTENT_LEVELS", "Research")
    registry_types: str = _env("TAP_REGISTRY_TYPES", "Archive")
    # ISO-8601 dates carried on the record; updated defaults to created
    registry_created: str = _env("TAP_REGISTRY_CREATED", "")
    registry_updated: str = _env("TAP_REGISTRY_UPDATED", "")

    # SKA SRC Permissions API, for the permissions-api plugin
    permissions_api_url: str = _env("TAP_PERMISSIONS_API_URL", "")
    permissions_service_name: str = _env("TAP_PERMISSIONS_SERVICE_NAME", "science-metadata")
    permissions_service_version: str = _env("TAP_PERMISSIONS_SERVICE_VERSION", "1")
    permissions_timeout_s: float = _float("TAP_PERMISSIONS_TIMEOUT", "10")


settings = Settings()
