"""PostgreSQL statistics, before/after deltas, and plan analysis.

The statistics views are cumulative, so a snapshot on its own says what the
server has done since it started — which is not what any single run did. Every
figure here is a delta across the measured window, computed per key, and the
absolute snapshots are kept beside it so the arithmetic can be checked.

Plans are captured *outside* the load. An EXPLAIN ANALYZE issued while sixty
clients are hammering the same tables measures the contention, not the plan;
run alone, it answers the question actually being asked, which is whether the
planner is choosing sensibly on this data at this size.
"""

from __future__ import annotations

import csv
import json
import logging
import time
import typing

import psycopg

log = logging.getLogger("egernia_bench.postgres")

# Each view with the columns that identify a row, so deltas subtract like from
# like. pg_stat_io is keyed by four columns and would otherwise collapse into
# nonsense.
VIEWS: dict[str, tuple[str, ...]] = {
    "pg_stat_database": ("datname",),
    "pg_stat_io": ("backend_type", "object", "context"),
    "pg_stat_user_tables": ("schemaname", "relname"),
    "pg_stat_user_indexes": ("schemaname", "relname", "indexrelname"),
    "pg_statio_user_tables": ("schemaname", "relname"),
    "pg_statio_user_indexes": ("schemaname", "relname", "indexrelname"),
}

STATEMENTS_COLUMNS = (
    "queryid",
    "calls",
    "total_exec_time",
    "mean_exec_time",
    "stddev_exec_time",
    "min_exec_time",
    "max_exec_time",
    "rows",
    "shared_blks_hit",
    "shared_blks_read",
    "shared_blks_dirtied",
    "shared_blks_written",
    "local_blks_hit",
    "local_blks_read",
    "temp_blks_read",
    "temp_blks_written",
    "shared_blk_read_time",
    "shared_blk_write_time",
    "temp_blk_read_time",
    "temp_blk_write_time",
    "wal_records",
    "wal_bytes",
    "query",
)


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=True)


def reset_statements(conn) -> None:
    """Zero pg_stat_statements so a run's window starts from nothing.

    Reset rather than delta'd for this one view: queryids come and go as
    statements age out of the shared table, and a delta across an eviction is
    a negative call count. Resetting makes the window unambiguous.
    """
    conn.execute("SELECT pg_stat_statements_reset()")


def snapshot(conn) -> dict:
    """Every statistics view, as rows of plain dicts."""
    out: dict[str, typing.Any] = {
        "captured_at": time.time(),
        # Recorded so the summary can find its own row in pg_stat_database.
        # That view has one row per database plus a shared-objects row whose
        # datname is NULL, and the NULL row sorts first — so "the first row"
        # is an empty accounting entry, not this database.
        "database": conn.execute("SELECT current_database()").fetchone()[0],
    }
    for view, _ in VIEWS.items():
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {view}")
                names = [d.name for d in cur.description]
                out[view] = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
        except psycopg.Error as exc:
            # pg_stat_io needs PG16+; a missing view is recorded rather than
            # fatal, so the suite still runs against an older server.
            log.warning("%s unavailable: %s", view, exc)
            out[view] = []
    out["settings"] = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, setting FROM pg_settings WHERE name = ANY(%s)",
            (
                [
                    "shared_buffers",
                    "work_mem",
                    "max_connections",
                    "effective_cache_size",
                    "track_io_timing",
                    "shared_preload_libraries",
                    "random_page_cost",
                    "max_parallel_workers_per_gather",
                    "jit",
                ],
            ),
        ).fetchall()
    }
    return out


def activity(conn) -> dict:
    """A point-in-time look at connections: how many, and how many blocked."""
    row = conn.execute(
        """
        SELECT count(*) FILTER (WHERE state = 'active')                AS active,
               count(*) FILTER (WHERE wait_event_type IS NOT NULL
                                  AND state = 'active')                AS waiting,
               count(*) FILTER (WHERE state = 'idle')                  AS idle,
               count(*) FILTER (WHERE state = 'idle in transaction')   AS idle_in_txn,
               count(*)                                               AS total,
               coalesce(max(extract(epoch FROM now() - query_start))
                        FILTER (WHERE state = 'active'), 0)            AS longest_active_s
          FROM pg_stat_activity
         WHERE datname = current_database()
        """
    ).fetchone()
    return {
        "t": time.time(),
        "active": row[0],
        "waiting": row[1],
        "idle": row[2],
        "idle_in_transaction": row[3],
        "total": row[4],
        "longest_active_seconds": float(row[5]),
    }


def statements(conn, limit: int = 500) -> list[dict]:
    columns = ", ".join(STATEMENTS_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {columns} FROM pg_stat_statements "
            f"WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
            f"ORDER BY total_exec_time DESC LIMIT {int(limit)}"
        )
        names = [d.name for d in cur.description]
        rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    for row in rows:
        calls = row.get("calls") or 0
        hit = row.get("shared_blks_hit") or 0
        read = row.get("shared_blks_read") or 0
        row["rows_per_call"] = (row.get("rows") or 0) / calls if calls else 0.0
        row["cache_hit_ratio"] = hit / (hit + read) if (hit + read) else None
        # 8 kB blocks: the byte figures the report quotes are derived here so
        # the CSV and the summary cannot disagree about them.
        row["shared_read_bytes"] = read * 8192
        row["shared_written_bytes"] = (row.get("shared_blks_written") or 0) * 8192
        row["temp_bytes"] = (
            (row.get("temp_blks_read") or 0) + (row.get("temp_blks_written") or 0)
        ) * 8192
    return rows


def write_statements_csv(rows: list[dict], path) -> None:
    if not rows:
        return
    fields = list(rows[0].keys())
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fields})


def delta(before: dict, after: dict) -> dict:
    """After minus before, per view, per key, for every numeric column."""
    result: dict[str, list[dict]] = {"database": after.get("database")}
    for view, keys in VIEWS.items():
        index = {tuple(str(row.get(k)) for k in keys): row for row in before.get(view, [])}
        rows = []
        for row in after.get(view, []):
            key = tuple(str(row.get(k)) for k in keys)
            base = index.get(key, {})
            entry = {k: row.get(k) for k in keys}
            for column, value in row.items():
                if column in keys or not isinstance(value, (int, float)):
                    continue
                previous = base.get(column)
                entry[column] = value - previous if isinstance(previous, (int, float)) else value
            rows.append(entry)
        result[view] = rows
    return result


def summarise(delta_rows: dict) -> dict:
    """The handful of database numbers the report leads with.

    The pg_stat_database row is selected by name. Taking the first row instead
    silently reported the shared-objects entry — datname NULL, a few hundred
    block accesses — as the workload's cache hit ratio and block reads, which
    made an I/O-bound run look like a perfect 100% hit rate.
    """
    name = delta_rows.get("database")
    rows = [r for r in delta_rows.get("pg_stat_database", []) if r.get("datname")]
    database = next((r for r in rows if r.get("datname") == name), None)
    if database is None:
        # No name recorded (an older run) or no matching row: the busiest named
        # database is a better guess than the first one, and is still wrong
        # loudly rather than quietly.
        database = max(
            rows, key=lambda r: (r.get("blks_hit") or 0) + (r.get("blks_read") or 0), default={}
        )
    hit = database.get("blks_hit") or 0
    read = database.get("blks_read") or 0
    tables = delta_rows.get("pg_stat_user_tables", [])
    io_rows = delta_rows.get("pg_stat_io", [])
    client_io = [r for r in io_rows if r.get("backend_type") == "client backend"]
    return {
        "transactions_committed": database.get("xact_commit"),
        "transactions_rolled_back": database.get("xact_rollback"),
        "blocks_hit": hit,
        "blocks_read": read,
        "cache_hit_ratio": hit / (hit + read) if (hit + read) else None,
        "tuples_returned": database.get("tup_returned"),
        "tuples_fetched": database.get("tup_fetched"),
        "temp_files": database.get("temp_files"),
        "temp_bytes": database.get("temp_bytes"),
        "deadlocks": database.get("deadlocks"),
        "blk_read_time_ms": database.get("blk_read_time"),
        "blk_write_time_ms": database.get("blk_write_time"),
        "sequential_scans": sum(t.get("seq_scan") or 0 for t in tables),
        "sequential_tuples_read": sum(t.get("seq_tup_read") or 0 for t in tables),
        "index_scans": sum(t.get("idx_scan") or 0 for t in tables),
        # Client backends only. pg_stat_io also counts the checkpointer,
        # background writer and autovacuum, and after a bulk load those dwarf
        # the query workload — which would attribute generation I/O to the
        # measurement that followed it.
        "io_read_bytes": sum((r.get("reads") or 0) * 8192 for r in client_io),
        "io_write_bytes": sum((r.get("writes") or 0) * 8192 for r in client_io),
        "io_read_time_ms": sum(r.get("read_time") or 0 for r in client_io),
        "io_write_time_ms": sum(r.get("write_time") or 0 for r in client_io),
        "io_read_bytes_all_backends": sum((r.get("reads") or 0) * 8192 for r in io_rows),
    }


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

# A table this size or larger has no business being sequentially scanned by a
# point or cone query. Below it, a seq scan is often the right plan and
# flagging it would be noise.
LARGE_TABLE_BYTES = 64 * 1024 * 1024
ROWS_REMOVED_FLAG = 100_000
ESTIMATE_RATIO_FLAG = 10.0
NESTED_LOOP_LOOPS_FLAG = 10_000
IO_TIME_FRACTION_FLAG = 0.5

# What each spatial class is expected to use. An index that exists, is
# expected, and is not used is the most valuable thing a plan capture can find
# — it is invisible in a latency number until the data grows.
# Classes whose whole purpose is to read the table. Flagging a sequential scan
# for an unfiltered aggregate would be crying wolf: it is the correct plan, and
# a flag that fires on correct plans stops being read.
FULL_SCAN_BY_DESIGN = frozenset({"Q13", "Q14"})

# A misestimate only matters if the node it is on costs something. On uniformly
# distributed synthetic data the planner is routinely an order of magnitude out
# on a node that runs in 40 microseconds, and reporting that as a finding
# buries the ones that do matter.
ESTIMATE_MIN_NODE_MS = 5.0

EXPECTED_INDEXES = {
    "Q02": "obscore_obs_id_idx",
    "Q03": "obscore_collection_type_idx",
    "Q04": "obscore_time_idx",
    "Q05": "obscore_spoint_gist",
    "Q06": "obscore_spoint_gist",
    "Q07": "obscore_spoint_gist",
    "Q12": "obscore_spoint_gist",
}


def _walk(node: dict) -> typing.Iterator[dict]:
    yield node
    for child in node.get("Plans", []) or []:
        yield from _walk(child)


def table_sizes(conn) -> dict[str, int]:
    return {
        row[0]: row[1]
        for row in conn.execute(
            """
            SELECT c.relname, pg_table_size(c.oid)
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname IN ('caom', 'ivoa') AND c.relkind = 'r'
            """
        ).fetchall()
    }


def flag_plan(plan: dict, query_class: str, sizes: dict[str, int]) -> list[dict]:
    """Everything about this plan that a human should look at."""
    flags: list[dict] = []
    root = plan["Plan"]
    total_ms = plan.get("Execution Time") or root.get("Actual Total Time") or 0.0
    indexes_used = set()

    for node in _walk(root):
        node_type = node.get("Node Type", "")
        relation = node.get("Relation Name", "")
        if "Index Name" in node:
            indexes_used.add(node["Index Name"])

        if (
            node_type == "Seq Scan"
            and sizes.get(relation, 0) >= LARGE_TABLE_BYTES
            and query_class not in FULL_SCAN_BY_DESIGN
        ):
            flags.append(
                {
                    "flag": "sequential_scan_on_large_table",
                    "detail": f"Seq Scan on {relation} ({sizes[relation] / 2**20:.0f} MiB)",
                    "severity": "high",
                }
            )

        removed = node.get("Rows Removed by Filter") or 0
        if removed >= ROWS_REMOVED_FLAG:
            flags.append(
                {
                    "flag": "large_rows_removed_by_filter",
                    "detail": f"{node_type} on {relation or '?'} discarded {removed:,} rows",
                    "severity": "high" if removed >= 10 * ROWS_REMOVED_FLAG else "medium",
                }
            )

        if node.get("Sort Method", "").startswith("external"):
            flags.append(
                {
                    "flag": "temporary_spill",
                    "detail": (
                        f"{node.get('Sort Method')} using "
                        f"{node.get('Sort Space Used', 0)} kB — work_mem too small "
                        "for this plan"
                    ),
                    "severity": "medium",
                }
            )
        if node.get("Peak Memory Usage") and node.get("Disk Usage"):
            flags.append(
                {
                    "flag": "temporary_spill",
                    "detail": f"{node_type} spilled {node['Disk Usage']} kB to disk",
                    "severity": "medium",
                }
            )

        planned = node.get("Plan Rows") or 0
        actual = node.get("Actual Rows") or 0
        node_ms = (node.get("Actual Total Time") or 0.0) * (node.get("Actual Loops") or 1)
        if planned and actual and node_ms >= ESTIMATE_MIN_NODE_MS:
            ratio = max(planned / actual, actual / planned)
            if ratio >= ESTIMATE_RATIO_FLAG:
                flags.append(
                    {
                        "flag": "bad_cardinality_estimate",
                        "detail": (
                            f"{node_type} on {relation or '?'}: planned {planned:,.0f}, "
                            f"actual {actual:,.0f} ({ratio:.0f}x out) on a node "
                            f"costing {node_ms:.0f} ms"
                        ),
                        "severity": "medium" if ratio < 100 else "high",
                    }
                )

        if node_type == "Nested Loop":
            loops = max((child.get("Actual Loops") or 0) for child in node.get("Plans", [{}]))
            if loops >= NESTED_LOOP_LOOPS_FLAG:
                flags.append(
                    {
                        "flag": "large_nested_loop",
                        "detail": f"Nested Loop inner side executed {loops:,} times",
                        "severity": "high",
                    }
                )

    io_ms = (root.get("I/O Read Time") or 0.0) + (root.get("I/O Write Time") or 0.0)
    if total_ms and io_ms / total_ms >= IO_TIME_FRACTION_FLAG:
        flags.append(
            {
                "flag": "high_io_time",
                "detail": f"{io_ms:.0f} ms of {total_ms:.0f} ms was I/O wait",
                "severity": "high",
            }
        )

    expected = EXPECTED_INDEXES.get(query_class)
    if expected and expected not in indexes_used:
        flags.append(
            {
                "flag": "expected_index_unused",
                "detail": (
                    f"{query_class} was expected to use {expected}; the plan used "
                    f"{sorted(indexes_used) or 'no index'}"
                ),
                "severity": "high",
            }
        )
    return flags


def explain(
    conn, entries, sizes: dict[str, int], translate, statement_timeout_ms: int = 120_000
) -> list[dict]:
    """Capture and flag a plan per corpus entry."""
    plans = []
    conn.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
    for entry in entries:
        try:
            sql = translate(entry.adql)
        except Exception as exc:
            plans.append(
                {
                    "query_class": entry.query_class,
                    "query_id": entry.query_id,
                    "error": f"translate: {type(exc).__name__}: {exc}",
                }
            )
            continue
        try:
            row = conn.execute(
                f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql.rstrip(';')}"
            ).fetchone()
            plan = row[0][0] if isinstance(row[0], list) else row[0]
        except psycopg.Error as exc:
            plans.append(
                {
                    "query_class": entry.query_class,
                    "query_id": entry.query_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        plans.append(
            {
                "query_class": entry.query_class,
                "query_id": entry.query_id,
                "adql": entry.adql,
                "sql": sql,
                "execution_ms": plan.get("Execution Time"),
                "planning_ms": plan.get("Planning Time"),
                "flags": flag_plan(plan, entry.query_class, sizes),
                "plan": plan,
            }
        )
    return plans


def write_plans(plans: list[dict], directory) -> dict:
    """One JSON file per plan, plus a flag tally for the report."""
    tally: dict[str, int] = {}
    for plan in plans:
        name = f"{plan['query_class']}-{plan['query_id']}.json"
        (directory / name).write_text(json.dumps(plan, indent=2, default=str))
        for flag in plan.get("flags", []):
            tally[flag["flag"]] = tally.get(flag["flag"], 0) + 1
    return tally
