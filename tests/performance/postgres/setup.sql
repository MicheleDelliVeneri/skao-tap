\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE SCHEMA IF NOT EXISTS perf;

DROP TABLE IF EXISTS perf.catalog;
CREATE TABLE perf.catalog (
    source_id bigint PRIMARY KEY,
    ra double precision NOT NULL,
    dec double precision NOT NULL,
    flux double precision NOT NULL,
    position spoint NOT NULL
);

INSERT INTO perf.catalog (source_id, ra, dec, flux, position)
SELECT
    id,
    mod(id * 137.50776405003785, 360.0),
    -90.0 + mod(id * 73.0, 180.0),
    0.001 + mod(id * 17.0, 100000.0) / 100.0,
    spoint(
        radians(mod(id * 137.50776405003785, 360.0)),
        radians(-90.0 + mod(id * 73.0, 180.0))
    )
FROM generate_series(1, :scale_rows) AS series(id);

CREATE INDEX perf_catalog_position_gist ON perf.catalog USING gist (position);
CREATE INDEX perf_catalog_flux_btree ON perf.catalog (flux);
ANALYZE perf.catalog;

INSERT INTO tap_schema.schemas (schema_name, description, schema_index)
VALUES ('perf', 'Synthetic performance-test catalogue', 900)
ON CONFLICT (schema_name) DO UPDATE SET description = EXCLUDED.description;

INSERT INTO tap_schema.tables (
    schema_name, table_name, table_type, description, table_index
)
VALUES (
    'perf', 'perf.catalog', 'table', 'Synthetic performance-test catalogue', 900
)
ON CONFLICT (table_name) DO UPDATE SET description = EXCLUDED.description;

DELETE FROM tap_schema.columns WHERE table_name = 'perf.catalog';
INSERT INTO tap_schema.columns (
    table_name, column_name, datatype, description, unit, ucd,
    indexed, principal, column_index
)
VALUES
    ('perf.catalog', 'source_id', 'long', 'Synthetic source identifier', NULL,
     'meta.id;meta.main', 1, 1, 1),
    ('perf.catalog', 'ra', 'double', 'ICRS right ascension', 'deg',
     'pos.eq.ra;meta.main', 0, 1, 2),
    ('perf.catalog', 'dec', 'double', 'ICRS declination', 'deg',
     'pos.eq.dec;meta.main', 0, 1, 3),
    ('perf.catalog', 'flux', 'double', 'Synthetic integrated flux', 'mJy',
     'phot.flux.density', 1, 0, 4);

SELECT count(*) AS loaded_rows FROM perf.catalog;
