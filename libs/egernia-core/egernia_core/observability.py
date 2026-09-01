"""Structured logging, request correlation and metrics, shared by the services.

Built on SRCNet's `ska-src-logging`, so records carry the same fields and JSON
shape as the rest of SRCNet rather than this service's own format.

Each metric earns its place by one rule: it is a number nobody should have to
reproduce locally with a profiler to see — a deployment reports it itself.
"""

import contextlib
import logging
import os
import re
import time
import uuid
from contextvars import ContextVar

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from .config import settings

#: One registry per process, passed to the library's endpoint helper rather
#: than using the global default, so tests can build a clean one.
REGISTRY = CollectorRegistry()

#: The id tying an HTTP request to the job it creates and to the executor that
#: runs it. Set per request; the executor sets it from the job row.
_request_id: ContextVar[str | None] = ContextVar("tap_request_id", default=None)

REQUEST_ID_HEADER = "X-Request-ID"

# -- metrics ----------------------------------------------------------------


def _pool_wait_buckets() -> tuple[float, ...]:
    """Histogram edges with a boundary at the pool timeout — and just past it.

    A timed-out acquire waits fractionally *longer* than the timeout, so
    without an edge there it lands in whatever wide bucket contains the
    timeout and quantile interpolation reads it as the bucket's middle —
    reporting waits longer than the timeout allows. The edge pair (t, 1.2t]
    pins those observations to within 20% of the truth whatever the
    configured timeout is.
    """
    timeout = float(settings.db_pool_timeout_s)
    edges = {0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0}
    edges.add(round(timeout, 3))
    edges.add(round(timeout * 1.2, 3))
    return tuple(sorted(edges))


DB_POOL_WAIT = Histogram(
    "tap_db_pool_wait_seconds",
    "Time spent waiting for a database connection from the pool.",
    registry=REGISTRY,
    buckets=_pool_wait_buckets(),
)

DB_POOL_EXHAUSTED = Counter(
    "tap_db_pool_exhausted_total",
    "Requests refused because no database connection became free in time.",
    registry=REGISTRY,
)

DB_CONNECTIONS_IN_USE = Gauge(
    "tap_db_connections_in_use",
    "Database connections currently checked out of the pool by this process.",
    registry=REGISTRY,
)

QUERY_DURATION = Histogram(
    "tap_query_duration_seconds",
    "Time to run a user query, from acquiring a connection until the work"
    " ends — including a query that was aborted and a stream the client"
    " abandoned.",
    ["kind"],  # sync or async
    registry=REGISTRY,
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 300.0),
)

JOBS_BY_PHASE = Gauge(
    "tap_jobs",
    'UWS jobs in the store, by phase. phase="QUEUED" is the queue\'s depth'
    " and what to scale on: it grows with the work outstanding, where the"
    " oldest job's age saturates once the queue is being drained at all.",
    ["phase"],
    registry=REGISTRY,
)

OLDEST_QUEUED_JOB = Gauge(
    "tap_oldest_queued_job_seconds",
    "Age of the oldest QUEUED job — how long the head of the queue has"
    " waited. A latency figure for dashboards and alerts, not a scaling"
    " signal: under steady drain it tops out near one job's service time"
    " however deep the queue behind it is."
    ' Scale on tap_jobs{phase="QUEUED"} instead.',
    registry=REGISTRY,
)

JOBS_COMPLETED = Counter(
    "tap_jobs_completed_total",
    "Jobs finished by an executor, by final phase.",
    ["phase"],
    registry=REGISTRY,
)

ADQL_TRANSLATION_HITS = Counter(
    "tap_adql_translation_cache_hits_total",
    "ADQL translations served from the in-process memo instead of parsing.",
    registry=REGISTRY,
)

ADQL_TRANSLATION_MISSES = Counter(
    "tap_adql_translation_cache_misses_total",
    "ADQL translations that had to parse. With the hits above this is the"
    " cache's hit rate under this deployment's real traffic. It is also"
    " the denominator tap_adql_slow_parses_total needs: a hit does not parse,"
    " so slow parses are per miss, not per request.",
    registry=REGISTRY,
)

ADQL_SLOW_PARSES = Counter(
    "tap_adql_slow_parses_total",
    "ADQL translations that parsed successfully only after falling back to"
    " full context. Translation is most of a request's CPU, so this rising"
    " means the fast parse path has quietly degraded. A query that is simply"
    " invalid does not count here — it is a 4xx, not a regression.",
    registry=REGISTRY,
)


# -- logging ----------------------------------------------------------------


def configure_logging(app_name: str) -> logging.Logger:
    """Configure structured logging for a service and return its logger.

    The level comes from this project's own ``TAP_LOG_LEVEL`` so one variable
    still controls it, while the library's ``LOG_FORMAT``/``LOG_COLORIZE`` and
    redaction settings stay available for anyone who wants them.
    """
    import sys

    from ska_src_logging import get_logger

    # JSON in a container, coloured console when a human is watching, unless
    # the operator has said otherwise
    os.environ.setdefault("LOG_FORMAT", "console" if sys.stderr.isatty() else "json")
    os.environ.setdefault("LOG_ENABLE_REDACTION", "true")
    return get_logger(app_name=app_name, level=settings.log_level.upper())


# -- request correlation ----------------------------------------------------


#: What a correlation id may contain. Generated ids are hex; ids from callers
#: are anything at all until checked, and they end up in a SQL comment, a
#: response header and the logs — so `*/`, `;`, and CR/LF are all exclusions
#: that matter rather than tidiness.
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def new_request_id() -> str:
    """A fresh correlation id, for a request that arrived without one."""
    return uuid.uuid4().hex


def safe_request_id(value: str | None) -> str | None:
    """The value if it is usable as a correlation id, else None.

    A caller's id is echoed in a header, written into a SQL comment and put in
    the logs. Accepting it verbatim would let `*/` close the comment early,
    CR/LF split a header or forge a log line — so an id that is not plainly an
    identifier is refused rather than escaped, and the caller gets a generated
    one instead.
    """
    if value and SAFE_REQUEST_ID.fullmatch(value):
        return value
    return None


@contextlib.contextmanager
def request_context(value: str | None):
    """Hold a correlation id for the duration of a block, then restore it.

    Reset rather than cleared, so nesting restores what was there rather than
    nothing. Both callers need this: the API must not attribute work after a
    response to the request that finished, and the executor's loop runs for the
    life of the pod, so a leftover id would label the next poll, the cleanup
    pass and any loop error with whichever job ran last.
    """
    token = _request_id.set(value)
    try:
        yield value
    finally:
        _request_id.reset(token)


def request_id() -> str | None:
    """The correlation id in scope, if any."""
    return _request_id.get()


def tag_sql(sql: str) -> str:
    """Tag a statement with the request that caused it.

    PostgreSQL keeps the comment in ``pg_stat_activity.query`` and in the
    server log, so a slow statement can be traced back to the request and the
    job — which is the point of carrying the id past the application at all.

    Appended rather than prefixed, following sqlcommenter: a statement that
    still *starts* with SELECT keeps working with everything that reads the
    beginning of a query, from EXPLAIN wrappers to log greps. The comment goes
    inside the statement, before any trailing semicolon, so it cannot be read
    as a second empty statement.
    """
    current = safe_request_id(request_id())
    if not current:
        # unset, or something that has no business in a comment: tag nothing
        # rather than trust that whoever set it checked
        return sql
    body = sql.rstrip()
    terminator = ""
    if body.endswith(";"):
        body, terminator = body[:-1].rstrip(), ";"
    # `current` has been through safe_request_id, so it cannot close the
    # comment or carry a newline
    return f"{body} /* rid={current} */{terminator}"


@contextlib.contextmanager
def pool_wait_timer():
    """Record how long acquiring a database connection took.

    Wrap the acquisition and nothing else: held time is not waiting. Recorded
    in a ``finally``, so a wait that ends in ``PoolTimeout`` is measured too —
    that is the longest wait there is, and the one the histogram exists for.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        DB_POOL_WAIT.observe(time.perf_counter() - started)
