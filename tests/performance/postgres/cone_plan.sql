EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON)
SELECT count(*)
FROM perf.catalog
WHERE position <@ scircle(
    spoint(radians(62.3), radians(-65.5)),
    radians(0.5)
);
