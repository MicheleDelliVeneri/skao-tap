SELECT
    queryid,
    calls,
    total_exec_time,
    mean_exec_time,
    min_exec_time,
    max_exec_time,
    rows,
    shared_blks_hit,
    shared_blks_read,
    temp_blks_read,
    temp_blks_written,
    blk_read_time,
    blk_write_time,
    query
FROM pg_stat_statements
WHERE query ILIKE '%perf.catalog%'
ORDER BY total_exec_time DESC;
