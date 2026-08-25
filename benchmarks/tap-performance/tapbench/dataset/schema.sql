-- Synthetic CAOM/ObsCore benchmark schema.
--
-- Shaped like the real thing rather than like a convenient table: five CAOM
-- levels with the fan-out that makes a join cost something, and an ObsCore
-- table at plane level with the wide text columns (s_region, access_url,
-- obs_publisher_did) that decide how many rows fit in a page. Row width is
-- half of what an I/O benchmark measures, so it is not padded and not
-- trimmed.
--
-- Every table is registered in TAP_SCHEMA at the end, because the service
-- refuses a query against a table it does not publish — an unregistered
-- table would benchmark the refusal path.

-- Required by the suite rather than by the service: shared_preload_libraries
-- loads the library, but the extension still has to exist in the database
-- before pg_stat_statements has any view to read or reset.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE SCHEMA IF NOT EXISTS caom;
CREATE SCHEMA IF NOT EXISTS ivoa;

-- ---------------------------------------------------------------------------
-- CAOM
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS caom.observation (
    obs_id            text PRIMARY KEY,
    collection        text        NOT NULL,
    telescope_name    text        NOT NULL,
    instrument_name   text        NOT NULL,
    target_name       text,
    intent            text        NOT NULL,
    obs_type          text        NOT NULL,
    proposal_id       text,
    sequence_number   bigint,
    meta_release      timestamptz,
    obs_release_date  timestamptz
);

CREATE TABLE IF NOT EXISTS caom.plane (
    plane_id              text PRIMARY KEY,
    obs_id                text NOT NULL,
    product_id            text NOT NULL,
    calib_level           smallint NOT NULL,
    data_product_type     text NOT NULL,
    time_bounds_lower     double precision,
    time_bounds_upper     double precision,
    time_exposure         double precision,
    energy_bounds_lower   double precision,
    energy_bounds_upper   double precision,
    position_ra           double precision,
    position_dec          double precision,
    position_sample_size  double precision,
    data_release          timestamptz
);

CREATE TABLE IF NOT EXISTS caom.artifact (
    artifact_id     text PRIMARY KEY,
    plane_id        text NOT NULL,
    uri             text NOT NULL,
    product_type    text NOT NULL,
    content_type    text NOT NULL,
    content_length  bigint NOT NULL,
    release_type    text
);

CREATE TABLE IF NOT EXISTS caom.part (
    part_id       text PRIMARY KEY,
    artifact_id   text NOT NULL,
    part_name     text NOT NULL,
    product_type  text NOT NULL
);

CREATE TABLE IF NOT EXISTS caom.chunk (
    chunk_id           text PRIMARY KEY,
    part_id            text NOT NULL,
    naxis              smallint,
    position_axis_1    smallint,
    position_axis_2    smallint,
    energy_axis        smallint,
    time_axis          smallint,
    polarization_axis  smallint,
    observable_axis    smallint,
    sample_size        double precision
);

-- ---------------------------------------------------------------------------
-- ObsCore, at plane level as the model says
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ivoa.obscore (
    obs_publisher_did  text PRIMARY KEY,
    obs_id             text NOT NULL,
    obs_collection     text NOT NULL,
    dataproduct_type   text NOT NULL,
    calib_level        smallint NOT NULL,
    target_name        text,
    facility_name      text,
    instrument_name    text,
    s_ra               double precision,
    s_dec              double precision,
    s_fov              double precision,
    s_region           text,
    s_resolution       double precision,
    t_min              double precision,
    t_max              double precision,
    t_exptime          double precision,
    t_resolution       double precision,
    em_min             double precision,
    em_max             double precision,
    em_res_power       double precision,
    o_ucd              text,
    pol_states         text,
    access_url         text,
    access_format      text,
    access_estsize     bigint
);

-- ---------------------------------------------------------------------------
-- Indexes
--
-- The spatial one is the reason this file has a comment. The ADQL translator
-- emits
--     spoint(RADIANS(s_ra), RADIANS(s_dec)) @ scircle(...)
-- so the index has to be on that *expression*: a GiST index on a stored
-- spoint column would never be considered, and every cone search in the
-- corpus would be a sequential scan over the whole table. Whether the
-- planner actually picks this up is checked by the suite rather than
-- assumed — see analyze/explain flags.
-- ---------------------------------------------------------------------------

-- The index the comment above is about. Its name is a contract: the suite's
-- EXPECTED_INDEXES (collect/postgres.py) asserts the cone-search classes
-- plan through it.
CREATE INDEX IF NOT EXISTS obscore_spoint_gist
    ON ivoa.obscore USING gist (spoint(RADIANS(s_ra), RADIANS(s_dec)));

CREATE INDEX IF NOT EXISTS observation_collection_idx
    ON caom.observation (collection);
CREATE INDEX IF NOT EXISTS observation_instrument_idx
    ON caom.observation (instrument_name);
CREATE INDEX IF NOT EXISTS observation_release_idx
    ON caom.observation (obs_release_date);

CREATE INDEX IF NOT EXISTS plane_obs_idx ON caom.plane (obs_id);
CREATE INDEX IF NOT EXISTS plane_calib_idx ON caom.plane (calib_level);
CREATE INDEX IF NOT EXISTS plane_time_idx
    ON caom.plane (time_bounds_lower, time_bounds_upper);

CREATE INDEX IF NOT EXISTS artifact_plane_idx ON caom.artifact (plane_id);
CREATE INDEX IF NOT EXISTS part_artifact_idx ON caom.part (artifact_id);
CREATE INDEX IF NOT EXISTS chunk_part_idx ON caom.chunk (part_id);

-- Extended statistics on the parent/child key pairs. A child key
-- functionally determines its parent key, which per-column statistics cannot
-- see; `dependencies` puts that in front of the planner for a conjunction of
-- equality quals on one table, and `ndistinct` sharpens group counts over the
-- pair. Both are single-table estimates.
--
-- They are not the fix for the 50x-477x cardinality misestimates on the
-- join-heavy classes (Q09, Q11, Q14): those nodes misestimate
-- `child.parent_key = parent.parent_key`, an equality across two relations,
-- and Postgres estimates join selectivity from per-column statistics on the
-- two sides without consulting extended statistics at all. That finding is
-- still open and needs a different lever. Populated by the VACUUM ANALYZE
-- the dataset build already runs.
CREATE STATISTICS IF NOT EXISTS caom.plane_keys_stx (ndistinct, dependencies)
    ON obs_id, plane_id FROM caom.plane;
CREATE STATISTICS IF NOT EXISTS caom.artifact_keys_stx (ndistinct, dependencies)
    ON plane_id, artifact_id FROM caom.artifact;
CREATE STATISTICS IF NOT EXISTS caom.part_keys_stx (ndistinct, dependencies)
    ON artifact_id, part_id FROM caom.part;
CREATE STATISTICS IF NOT EXISTS caom.chunk_keys_stx (ndistinct, dependencies)
    ON part_id, chunk_id FROM caom.chunk;

CREATE INDEX IF NOT EXISTS obscore_obs_id_idx ON ivoa.obscore (obs_id);
CREATE INDEX IF NOT EXISTS obscore_collection_type_idx
    ON ivoa.obscore (obs_collection, dataproduct_type);
CREATE INDEX IF NOT EXISTS obscore_calib_idx ON ivoa.obscore (calib_level);
CREATE INDEX IF NOT EXISTS obscore_time_idx ON ivoa.obscore (t_min, t_max);
CREATE INDEX IF NOT EXISTS obscore_instrument_idx
    ON ivoa.obscore (instrument_name);

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

GRANT USAGE ON SCHEMA caom, ivoa TO tap_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA caom, ivoa TO tap_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA caom GRANT SELECT ON TABLES TO tap_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA ivoa GRANT SELECT ON TABLES TO tap_reader;

-- ---------------------------------------------------------------------------
-- TAP_SCHEMA registration
-- ---------------------------------------------------------------------------

INSERT INTO tap_schema.schemas (schema_name, description, schema_index) VALUES
    ('caom',  'Synthetic CAOM benchmark data', 50),
    ('ivoa',  'Synthetic ObsCore benchmark data', 51)
ON CONFLICT (schema_name) DO NOTHING;

INSERT INTO tap_schema.tables (schema_name, table_name, table_type, description, table_index) VALUES
    ('caom', 'caom.observation', 'table', 'Synthetic CAOM observations',  50),
    ('caom', 'caom.plane',       'table', 'Synthetic CAOM planes',        51),
    ('caom', 'caom.artifact',    'table', 'Synthetic CAOM artifacts',     52),
    ('caom', 'caom.part',        'table', 'Synthetic CAOM parts',         53),
    ('caom', 'caom.chunk',       'table', 'Synthetic CAOM chunks',        54),
    ('ivoa', 'ivoa.obscore',     'table', 'Synthetic ObsCore view of the planes', 55)
ON CONFLICT (table_name) DO NOTHING;
