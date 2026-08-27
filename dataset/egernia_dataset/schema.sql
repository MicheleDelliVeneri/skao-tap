-- What the benchmark adds on top of the schema the service already owns.
--
-- The tables themselves are NOT created here. The ODP and software metadata
-- plugins generate srcnet.* from their pydantic models at API bootstrap, and
-- ivoa.obscore is the ObsCore plugin's view over them. That is the schema
-- being benchmarked, so the suite loads it rather than defining a parallel
-- copy: a hand-maintained duplicate of a generated schema drifts the first
-- time the model gains a field, and then the numbers describe a table the
-- service does not serve.
--
-- This file therefore holds only what a *benchmark* needs and a deployment
-- does not: the statistics extension, indexes for the corpus's filter
-- columns, and grants.

-- Required by the suite rather than by the service: shared_preload_libraries
-- loads the library, but the extension still has to exist in the database
-- before pg_stat_statements has any view to read or reset.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- ---------------------------------------------------------------------------
-- Indexes for the corpus's filter columns
--
-- The service creates the primary keys and the two GiST indexes (the
-- s_region_geom footprint and the spoint(s_ra, s_dec) position). These are the
-- b-trees the query corpus needs on top: every one backs a WHERE or ORDER BY
-- that appears in config/queries.yaml, and without them the filter classes
-- measure a sequential scan rather than a lookup.
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS data_products_type_calib_idx
    ON srcnet.data_products (dataproduct_type, calib_level);
CREATE INDEX IF NOT EXISTS data_products_calib_idx
    ON srcnet.data_products (calib_level);
CREATE INDEX IF NOT EXISTS data_products_time_idx
    ON srcnet.data_products (t_min, t_max);
CREATE INDEX IF NOT EXISTS data_products_target_idx
    ON srcnet.data_products (target_name);
CREATE INDEX IF NOT EXISTS observations_collection_idx
    ON srcnet.observations (collection);
CREATE INDEX IF NOT EXISTS observations_instrument_idx
    ON srcnet.observations (instrument_name);

-- Extended statistics on each level's key chain.
--
-- A child key functionally determines its parent key, which per-column
-- statistics cannot see; `dependencies` puts that in front of the planner for
-- a conjunction of equality quals on one table, and `ndistinct` sharpens group
-- counts over the set. Both are single-table estimates.
--
-- They are not the fix for the cardinality misestimates on the join-heavy
-- classes: those nodes misestimate `child.parent_key = parent.parent_key`, an
-- equality across two relations, and Postgres estimates join selectivity from
-- per-column statistics on the two sides without consulting extended
-- statistics at all. That finding is still open and needs a different lever.
-- Populated by the VACUUM ANALYZE the dataset build already runs.
--
-- The ODP chain is wider than a two-column parent/child pair: a data product
-- is keyed by (project, obs, sbd, eb, product), and every prefix of that
-- determines the next.
CREATE STATISTICS IF NOT EXISTS srcnet.observations_keys_stx (ndistinct, dependencies)
    ON project_id, obs_id FROM srcnet.observations;
CREATE STATISTICS IF NOT EXISTS srcnet.scheduling_blocks_keys_stx (ndistinct, dependencies)
    ON project_id, obs_id, sbd_id FROM srcnet.scheduling_blocks;
CREATE STATISTICS IF NOT EXISTS srcnet.execution_blocks_keys_stx (ndistinct, dependencies)
    ON project_id, obs_id, sbd_id, eb_id FROM srcnet.execution_blocks;
CREATE STATISTICS IF NOT EXISTS srcnet.data_products_keys_stx (ndistinct, dependencies)
    ON project_id, obs_id, sbd_id, eb_id, product_id FROM srcnet.data_products;
CREATE STATISTICS IF NOT EXISTS srcnet.artifacts_keys_stx (ndistinct, dependencies)
    ON project_id, obs_id, sbd_id, eb_id, product_id, artifact_id FROM srcnet.artifacts;

-- ---------------------------------------------------------------------------
-- Grants
--
-- The service downgrades to tap_reader before running user SQL, and that role
-- is granted rights on the schemas that existed when the database was
-- initialised. A schema added afterwards is invisible to it — every query
-- against these tables fails with "permission denied for schema" until this
-- runs, which is a property of any deployment adding a metadata domain, not
-- just of this benchmark.
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA srcnet, ivoa TO tap_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA srcnet, ivoa TO tap_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA srcnet GRANT SELECT ON TABLES TO tap_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA ivoa GRANT SELECT ON TABLES TO tap_reader;
