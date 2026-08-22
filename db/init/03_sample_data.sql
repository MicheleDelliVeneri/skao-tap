-- Example science schema: a small radio continuum source catalogue.
CREATE SCHEMA IF NOT EXISTS ska;

CREATE TABLE ska.continuum_sources (
    source_id       bigint PRIMARY KEY,
    source_name     text NOT NULL,
    ra              double precision NOT NULL,   -- deg, ICRS
    "dec"           double precision NOT NULL,   -- deg, ICRS
    flux_peak       double precision,            -- mJy/beam
    flux_int        double precision,            -- mJy
    maj_axis        double precision,            -- arcsec
    min_axis        double precision,            -- arcsec
    pos_angle       double precision,            -- deg
    spectral_index  double precision,
    obs_date        timestamptz
);

CREATE INDEX continuum_sources_radec ON ska.continuum_sources (ra, "dec");

INSERT INTO ska.continuum_sources VALUES
    (1, 'SKA-CS J0408-6545', 62.0850, -65.7525, 152.3, 168.9, 12.1,  8.4,  34.0, -0.72, '2026-01-12T04:31:00Z'),
    (2, 'SKA-CS J0409-6522', 62.4412, -65.3711,  48.7,  52.0,  9.8,  7.9, 121.5, -0.81, '2026-01-12T04:31:00Z'),
    (3, 'SKA-CS J0413-6528', 63.3401, -65.4790,  12.9,  13.5,  8.1,  7.2,  75.2, -0.65, '2026-01-12T04:31:00Z'),
    (4, 'SKA-CS J1331+3030', 202.7845, 30.5091, 14700.0, 14930.0, 22.7, 18.9, 10.0, -0.55, '2026-02-03T21:14:00Z'),
    (5, 'SKA-CS J1959+4044', 299.8682,  40.7339, 21100.0, 22050.0, 30.2, 25.1, 45.3, -0.78, '2026-02-03T21:14:00Z'),
    (6, 'SKA-CS J0521-3654',  80.2921, -36.9101,  33.4,  35.8, 10.5,  9.0,  15.8, -0.70, '2026-03-19T02:02:00Z'),
    (7, 'SKA-CS J0522-3627',  80.7416, -36.4586, 6540.0, 6720.0, 14.4, 12.6,  88.1, -0.44, '2026-03-19T02:02:00Z'),
    (8, 'SKA-CS J0525-3612',  81.4400, -36.2000,   5.2,   5.4,  7.7,  7.1,  60.0, -0.90, '2026-03-19T02:02:00Z');

-- Register the science table in TAP_SCHEMA.
INSERT INTO tap_schema.schemas (schema_name, description, schema_index) VALUES
    ('ska', 'Example SKAO science tables', 2);

INSERT INTO tap_schema.tables (schema_name, table_name, table_type, description, table_index) VALUES
    ('ska', 'ska.continuum_sources', 'table', 'Example radio continuum source catalogue', 1);

INSERT INTO tap_schema.columns (table_name, column_name, datatype, arraysize, xtype, description, unit, ucd, indexed, principal, std, column_index) VALUES
    ('ska.continuum_sources', 'source_id',      'long',   NULL, NULL,        'unique source identifier',        NULL,     'meta.id;meta.main',      1, 1, 0, 1),
    ('ska.continuum_sources', 'source_name',    'char',   '*',  NULL,        'IAU-style source name',           NULL,     'meta.id',                0, 1, 0, 2),
    ('ska.continuum_sources', 'ra',             'double', NULL, NULL,        'Right ascension (ICRS)',          'deg',    'pos.eq.ra;meta.main',    1, 1, 0, 3),
    ('ska.continuum_sources', 'dec',            'double', NULL, NULL,        'Declination (ICRS)',              'deg',    'pos.eq.dec;meta.main',   1, 1, 0, 4),
    ('ska.continuum_sources', 'flux_peak',      'double', NULL, NULL,        'Peak flux density',               'mJy/beam','phot.flux.density;em.radio', 0, 1, 0, 5),
    ('ska.continuum_sources', 'flux_int',       'double', NULL, NULL,        'Integrated flux density',         'mJy',    'phot.flux.density;em.radio', 0, 1, 0, 6),
    ('ska.continuum_sources', 'maj_axis',       'double', NULL, NULL,        'Fitted major axis',               'arcsec', 'phys.angSize.smajAxis',  0, 0, 0, 7),
    ('ska.continuum_sources', 'min_axis',       'double', NULL, NULL,        'Fitted minor axis',               'arcsec', 'phys.angSize.sminAxis',  0, 0, 0, 8),
    ('ska.continuum_sources', 'pos_angle',      'double', NULL, NULL,        'Fitted position angle',           'deg',    'pos.posAng',             0, 0, 0, 9),
    ('ska.continuum_sources', 'spectral_index', 'double', NULL, NULL,        'Fitted spectral index',           NULL,     'spect.index',            0, 0, 0, 10),
    ('ska.continuum_sources', 'obs_date',       'char',   '*',  'timestamp', 'Observation date',                NULL,     'time.epoch',             0, 0, 0, 11);
