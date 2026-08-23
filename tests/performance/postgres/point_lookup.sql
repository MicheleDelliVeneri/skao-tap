\set source_id random(1, :scale_rows)
SELECT source_id, ra, dec, flux
FROM perf.sources
WHERE source_id = :source_id;
