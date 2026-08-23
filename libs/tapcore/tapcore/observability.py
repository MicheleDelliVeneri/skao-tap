"""Structured logging, request correlation and metrics, shared by the services.

Built on SRCNet's `ska-src-logging`, so records carry the same fields and JSON
shape as the rest of SRCNet rather than this service's own format.

The metrics here are chosen from what recent performance work actually needed
and did not have. Diagnosing a total collapse above eight concurrent queries
took a local reproduction and a sampling profiler, because nothing in a
deployment could have shown it: the signal was time spent waiting for a
database connection, and that is now ``tap_db_pool_wait_seconds``. The rest
follow the same rule — a number nobody would have to reproduce locally to see.
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

DB_POOL_WAIT = Histogram(
    "tap_db_pool_wait_seconds",
    "Time spent waiting for a database connection from the pool.",
    registry=REGISTRY,
    # sub-millisecond when the pool is healthy; the tail is what matters, so
    # the buckets reach past the pool timeout
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
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
    "UWS jobs in the store, by phase.",
    ["phase"],
    registry=REGISTRY,
)

OLDEST_QUEUED_JOB = Gauge(
    "tap_oldest_queued_job_seconds",
    "Age of the oldest QUEUED job — the queue's backlog, and what to scale on.",
    registry=REGISTRY,
)

JOBS_COMPLETED = Counter(
    "tap_jobs_completed_total",
    "Jobs finished by an executor, by final phase.",
    ["phase"],
    registry=REGISTRY,
)

ADQL_SLOW_PARSES = Counter(
    "tap_adql_slow_parses_total",
    "ADQL translations that parsed successfully only after falling back to"
    " full context. Translation is most of a request's CPU and the fast path"
    " is 35x cheaper, so this rising means the service has quietly returned to"
    " its old ceiling. A query that is simply invalid does not count here — it"
    " is a 4xx, not a regression.",
    registry=REGISTRY,
)


# -- logging ----------------------------------------------------------------


def configure_logging(app_name: str) -> logging.Logger:
    """Configure structured logging for a service and return its logger.

    The level comes from this project's own ``TAP_LOG_LEVEL`` so one variable
    still controls it, while the library's ``LOG_FORMAT``/``LOG_COLORIZE`` and
    redaction settings stay available for anyone who wants them.
    """
    from ska_src_logging import get_logger

    # JSON in a container, coloured console when a human is watching, unless
    # the operator has said otherwise
    os.environ.setdefault("LOG_FORMAT", "console" if _looks_interactive() else "json")
    os.environ.setdefault("LOG_ENABLE_REDACTION", "true")
    return get_logger(app_name=app_name, level=settings.log_level.upper())


def _looks_interactive() -> bool:
    import sys

    return sys.stderr.isatty()


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


def set_request_id(value: str | None) -> None:
    _request_id.set(value)


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
