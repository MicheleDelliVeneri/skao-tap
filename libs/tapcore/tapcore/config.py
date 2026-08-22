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


settings = Settings()
