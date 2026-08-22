"""Shared query preparation and synchronous execution."""

from tapcore.adql import adql_to_postgresql, apply_maxrec, check_language, touched_tables
from tapcore.config import settings
from tapcore.db import pool
from tapcore.errors import UsageError
from tapcore.votable import normalize_format, serialize

from .params import require


def prepare_query(params: dict[str, str]) -> dict:
    """Validate TAP parameters and translate the ADQL query.

    Returns a dict with the translated sql, maxrec, and output format info.
    """
    request = params.get("REQUEST")  # required in TAP 1.0, deprecated in 1.1
    if request and request not in ("doQuery",):
        raise UsageError(f"REQUEST={request} is not supported")
    if "UPLOAD" in params:
        raise UsageError("table upload (UPLOAD) is not supported by this service")

    check_language(params.get("LANG", "ADQL"))
    query = require(params, "QUERY")
    sql = adql_to_postgresql(query)

    published = _published_tables()
    for table in touched_tables(sql):
        if table.lower() not in published:
            raise UsageError(f"table {table} is not published by this service")

    maxrec = params.get("MAXREC")
    if maxrec is not None:
        try:
            maxrec = int(maxrec)
        except ValueError:
            raise UsageError(f"MAXREC={maxrec} is not an integer") from None
        if maxrec < 0:
            raise UsageError("MAXREC must be >= 0")
        maxrec = min(maxrec, settings.hard_maxrec)
    else:
        maxrec = settings.default_maxrec

    fmt = params.get("RESPONSEFORMAT") or params.get("FORMAT")
    try:
        fmt_key, mime, ext = normalize_format(fmt)
    except ValueError as exc:
        raise UsageError(str(exc)) from None

    return {
        "sql": sql,
        "maxrec": maxrec,
        "fmt_key": fmt_key,
        "mime": mime,
        "ext": ext,
    }


def _published_tables() -> set[str]:
    with pool().connection() as conn:
        rows = conn.execute("SELECT table_name FROM tap_schema.tables").fetchall()
    return {r[0].lower() for r in rows}


def run_sync(prepared: dict) -> tuple[bytes, str]:
    """Execute the prepared query and serialize the result."""
    sql = apply_maxrec(prepared["sql"], prepared["maxrec"])
    with pool().connection() as conn, conn.transaction():
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(settings.sync_timeout_s * 1000),),
        )
        conn.execute(f"SET LOCAL ROLE {settings.query_role}")
        cur = conn.execute(sql)
        names = [d.name for d in cur.description]
        rows = cur.fetchall()
    status = "OK"
    if len(rows) > prepared["maxrec"]:
        rows = rows[: prepared["maxrec"]]
        status = "OVERFLOW"
    return serialize(names, rows, prepared["fmt_key"], status), prepared["mime"]
