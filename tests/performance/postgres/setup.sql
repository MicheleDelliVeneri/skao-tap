\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE SCHEMA IF NOT EXISTS perf;

-- Not perf.catalog: CATALOG is reserved in the ADQL grammar, so every query
-- naming it fails to parse before it reaches the publication check, and the
-- HTTP workloads all error out.
DROP TABLE IF EXISTS perf.sources;
CREATE TABLE perf.sources (
    source_id bigint PRIMARY KEY,
    ra double precision NOT NULL,
    dec double precision NOT NULL,
    flux double precision NOT NULL,
    position spoint NOT NULL
);

INSERT INTO perf.sources (source_id, ra, dec, flux, position)
SELECT
    id,
    mod(id * 137, 360)::double precision
        + mod(id, 1000)::double precision / 1000.0,
    -90.0 + mod(id * 73, 180)::double precision,
    0.001 + mod(id * 17, 100000)::double precision / 100.0,
    spoint(
        radians(
            mod(id * 137, 360)::double precision
                + mod(id, 1000)::double precision / 1000.0
        ),
        radians(-90.0 + mod(id * 73, 180)::double precision)
    )
FROM generate_series(1, :scale_rows) AS series(id);

-- The cone workload filters on spoint(radians(ra), radians(dec)), so this
-- expression index is the one that serves it; an index on the position column
-- would only add build time and storage to every run.
CREATE INDEX perf_sources_radec_gist ON perf.sources
USING gist (spoint(radians(ra), radians(dec)));
CREATE INDEX perf_sources_flux_btree ON perf.sources (flux);
ANALYZE perf.sources;
GRANT USAGE ON SCHEMA perf TO tap_reader;
GRANT SELECT ON perf.sources TO tap_reader;

INSERT INTO tap_schema.schemas (schema_name, description, schema_index)
VALUES ('perf', 'Synthetic performance-test catalogue', 900)
ON CONFLICT (schema_name) DO UPDATE SET description = EXCLUDED.description;

INSERT INTO tap_schema.tables (
    schema_name, table_name, table_type, description, table_index
)
VALUES (
    'perf', 'perf.sources', 'table', 'Synthetic performance-test catalogue', 900
)
ON CONFLICT (table_name) DO UPDATE SET description = EXCLUDED.description;

DELETE FROM tap_schema.columns WHERE table_name = 'perf.sources';
INSERT INTO tap_schema.columns (
    table_name, column_name, datatype, description, unit, ucd,
    indexed, principal, column_index
)
VALUES
    ('perf.sources', 'source_id', 'long', 'Synthetic source identifier', NULL,
     'meta.id;meta.main', 1, 1, 1),
    ('perf.sources', 'ra', 'double', 'ICRS right ascension', 'deg',
     'pos.eq.ra;meta.main', 0, 1, 2),
    ('perf.sources', 'dec', 'double', 'ICRS declination', 'deg',
     'pos.eq.dec;meta.main', 0, 1, 3),
    ('perf.sources', 'flux', 'double', 'Synthetic integrated flux', 'mJy',
     'phot.flux.density', 1, 0, 4);

SELECT count(*) AS loaded_rows FROM perf.sources;
