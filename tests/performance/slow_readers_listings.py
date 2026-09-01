"""Reproduce the focused slow-reader and collection-listing capacity check."""

import argparse
import csv
import json
import os
import platform
import statistics
import tempfile
import threading
import time
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool


def median_query(conn, sql, params=(), repeats=5):
    samples = []
    rows = 0
    for _ in range(repeats):
        started = time.perf_counter()
        # pi-lens-ignore: python-sql-injection -- callers pass fixed benchmark SQL
        rows = len(conn.execute(sql, params).fetchall())
        samples.append((time.perf_counter() - started) * 1000)
    return rows, statistics.median(samples)


def listing_results(dsn):
    with psycopg.connect(dsn) as conn, conn.transaction(force_rollback=True):
        conn.execute(
            """INSERT INTO uws.jobs (job_id, phase, creation_time)
            SELECT 'scale-' || i,
                   CASE WHEN i % 9 = 0 THEN 'ARCHIVED' ELSE 'COMPLETED' END,
                   now() - i * interval '1 millisecond'
            FROM generate_series(1, 100000) i"""
        )
        conn.execute("ANALYZE uws.jobs")
        bounded = (
            "SELECT job_id FROM uws.jobs WHERE phase <> 'ARCHIVED' "
            "ORDER BY creation_time DESC LIMIT %s"
        )
        unbounded = (
            "SELECT job_id FROM uws.jobs WHERE phase <> 'ARCHIVED' ORDER BY creation_time DESC"
        )
        results = {
            "uws_default": median_query(conn, bounded, (100,)),
            "uws_maximum": median_query(conn, bounded, (1000,)),
            "uws_unbounded": median_query(conn, unbounded, repeats=3),
        }

        conn.execute("CREATE TEMP TABLE scale_documents (id text PRIMARY KEY, payload jsonb)")
        conn.execute(
            "CREATE TEMP TABLE scale_children "
            "(id text NOT NULL REFERENCES scale_documents, child_no int, "
            "PRIMARY KEY(id, child_no))"
        )
        conn.execute(
            "INSERT INTO scale_documents "
            "SELECT lpad(i::text, 8, '0'), '{}'::jsonb FROM generate_series(1, 100000) i"
        )
        conn.execute(
            "INSERT INTO scale_children SELECT lpad(i::text, 8, '0'), child_no "
            "FROM generate_series(1, 100000) i CROSS JOIN generate_series(1, 2) child_no"
        )
        conn.execute("ANALYZE scale_documents")
        conn.execute("ANALYZE scale_children")
        select = (
            "SELECT to_jsonb(p), "
            "(SELECT count(*) FROM scale_children c WHERE c.id = p.id) "
            "FROM scale_documents p{} ORDER BY p.id LIMIT %s"
        )
        results["metadata_first"] = median_query(conn, select.format(""), (101,))
        results["metadata_cursor"] = median_query(
            conn, select.format(" WHERE p.id > %s"), ("00050000", 101)
        )
        return results


def slow_reader_results(dsn):
    pool = ConnectionPool(dsn, min_size=0, max_size=2, open=True)

    def point_wait(readers):
        started = time.perf_counter()
        with pool.connection() as conn:
            conn.execute("SELECT 1").fetchone()
        elapsed = (time.perf_counter() - started) * 1000
        for reader in readers:
            reader.join()
        return elapsed

    barrier = threading.Barrier(3)

    def retained_reader():
        with pool.connection() as conn:
            rows = conn.execute("SELECT i FROM generate_series(1, 100) i").fetchall()
            barrier.wait()
            for _ in rows:
                time.sleep(0.005)

    readers = [threading.Thread(target=retained_reader) for _ in range(2)]
    for reader in readers:
        reader.start()
    barrier.wait()
    retained = point_wait(readers)

    barrier = threading.Barrier(3)

    def spooled_reader():
        with tempfile.SpooledTemporaryFile(max_size=1024) as spool:
            with pool.connection() as conn:
                for row in conn.execute("SELECT repeat('x', 128) FROM generate_series(1, 100)"):
                    spool.write(row[0].encode())
            spool.seek(0)
            barrier.wait()
            while spool.read(128):
                time.sleep(0.005)

    readers = [threading.Thread(target=spooled_reader) for _ in range(2)]
    for reader in readers:
        reader.start()
    barrier.wait()
    spooled = point_wait(readers)
    pool.close()
    return retained, spooled


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-url",
        default=os.getenv("TAP_DATABASE_URL", "postgresql://tap:tap@127.0.0.1:5432/tap"),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    results = listing_results(args.database_url)
    retained, spooled = slow_reader_results(args.database_url)
    rows = [
        ("slow_reader_retained", 2, retained),
        ("slow_reader_spooled", 2, spooled),
        *((name, count, elapsed) for name, (count, elapsed) in results.items()),
    ]
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "summary.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("measurement", "rows_or_readers", "median_ms"))
        writer.writerows((name, count, f"{elapsed:.2f}") for name, count, elapsed in rows)
    with psycopg.connect(args.database_url) as conn:
        version_row = conn.execute("SELECT version()").fetchone()
        assert version_row is not None
        postgres_version = version_row[0]
    (args.output / "environment.json").write_text(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "postgres": postgres_version,
                "uws_jobs": 100000,
                "metadata_roots": 100000,
                "metadata_children": 200000,
                "repetitions": 5,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
