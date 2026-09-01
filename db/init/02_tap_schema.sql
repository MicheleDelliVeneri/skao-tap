-- TAP_SCHEMA as mandated by TAP 1.1 (section 4): self-describing metadata
-- tables that are themselves queryable through ADQL.
CREATE SCHEMA IF NOT EXISTS tap_schema;

CREATE TABLE tap_schema.schemas (
    schema_name   text PRIMARY KEY,
    utype         text,
    description   text,
    schema_index  integer
);

CREATE TABLE tap_schema.tables (
    schema_name  text NOT NULL REFERENCES tap_schema.schemas (schema_name),
    table_name   text PRIMARY KEY,
    table_type   text NOT NULL DEFAULT 'table',
    utype        text,
    description  text,
    table_index  integer
);

CREATE TABLE tap_schema.columns (
    table_name    text NOT NULL REFERENCES tap_schema.tables (table_name),
    column_name   text NOT NULL,
    datatype      text NOT NULL,
    arraysize     text,
    xtype         text,
    "size"        integer,
    description   text,
    utype         text,
    unit          text,
    ucd           text,
    indexed       integer NOT NULL DEFAULT 0,
    principal     integer NOT NULL DEFAULT 0,
    std           integer NOT NULL DEFAULT 0,
    column_index  integer,
    PRIMARY KEY (table_name, column_name)
);

CREATE TABLE tap_schema.keys (
    key_id        text PRIMARY KEY,
    from_table    text NOT NULL REFERENCES tap_schema.tables (table_name),
    target_table  text NOT NULL REFERENCES tap_schema.tables (table_name),
    utype         text,
    description   text
);

CREATE TABLE tap_schema.key_columns (
    key_id         text NOT NULL REFERENCES tap_schema.keys (key_id),
    from_column    text NOT NULL,
    target_column  text NOT NULL
);

-- ---------------------------------------------------------------------------
-- Self-description of TAP_SCHEMA itself
-- ---------------------------------------------------------------------------
INSERT INTO tap_schema.schemas (schema_name, description, schema_index) VALUES
    ('tap_schema', 'TAP standard metadata tables', 1);

INSERT INTO tap_schema.tables (schema_name, table_name, table_type, description, table_index) VALUES
    ('tap_schema', 'tap_schema.schemas',     'table', 'Schemas published by this service', 1),
    ('tap_schema', 'tap_schema.tables',      'table', 'Tables published by this service', 2),
    ('tap_schema', 'tap_schema.columns',     'table', 'Columns of the published tables', 3),
    ('tap_schema', 'tap_schema.keys',        'table', 'Foreign keys between published tables', 4),
    ('tap_schema', 'tap_schema.key_columns', 'table', 'Columns of the foreign keys', 5);

INSERT INTO tap_schema.columns (table_name, column_name, datatype, arraysize, description, principal, std, column_index) VALUES
    ('tap_schema.schemas', 'schema_name',  'char', '*', 'schema name',                  1, 1, 1),
    ('tap_schema.schemas', 'utype',        'char', '*', 'utype if schema maps a model', 0, 1, 2),
    ('tap_schema.schemas', 'description',  'char', '*', 'brief description',            0, 1, 3),
    ('tap_schema.schemas', 'schema_index', 'int',  NULL, 'recommended sort order',      0, 1, 4),
    ('tap_schema.tables', 'schema_name', 'char', '*', 'schema the table belongs to', 1, 1, 1),
    ('tap_schema.tables', 'table_name',  'char', '*', 'fully qualified table name',  1, 1, 2),
    ('tap_schema.tables', 'table_type',  'char', '*', 'table or view',               0, 1, 3),
    ('tap_schema.tables', 'utype',       'char', '*', 'utype if table maps a model', 0, 1, 4),
    ('tap_schema.tables', 'description', 'char', '*', 'brief description',           0, 1, 5),
    ('tap_schema.tables', 'table_index', 'int',  NULL, 'recommended sort order',     0, 1, 6),
    ('tap_schema.columns', 'table_name',   'char', '*', 'table the column belongs to',            1, 1, 1),
    ('tap_schema.columns', 'column_name',  'char', '*', 'column name',                            1, 1, 2),
    ('tap_schema.columns', 'datatype',     'char', '*', 'ADQL datatype',                          0, 1, 3),
    ('tap_schema.columns', 'arraysize',    'char', '*', 'VOTable arraysize',                      0, 1, 4),
    ('tap_schema.columns', 'xtype',        'char', '*', 'extended type',                          0, 1, 5),
    -- delimited: SIZE is an ADQL reserved word, and TAP 1.1 requires the
    -- name registered exactly as a query must write it
    ('tap_schema.columns', '"size"',       'int',  NULL, 'deprecated: use arraysize',             0, 1, 6),
    ('tap_schema.columns', 'description',  'char', '*', 'brief description',                      0, 1, 7),
    ('tap_schema.columns', 'utype',        'char', '*', 'utype if column maps a model',           0, 1, 8),
    ('tap_schema.columns', 'unit',         'char', '*', 'unit (VOUnits)',                         0, 1, 9),
    ('tap_schema.columns', 'ucd',          'char', '*', 'UCD of the column',                      0, 1, 10),
    ('tap_schema.columns', 'indexed',      'int',  NULL, '1 if indexed',                          0, 1, 11),
    ('tap_schema.columns', 'principal',    'int',  NULL, '1 if principal column',                 0, 1, 12),
    ('tap_schema.columns', 'std',          'int',  NULL, '1 if defined by a standard',            0, 1, 13),
    ('tap_schema.columns', 'column_index', 'int',  NULL, 'recommended sort order',                0, 1, 14),
    ('tap_schema.keys', 'key_id',       'char', '*', 'unique key identifier', 1, 1, 1),
    ('tap_schema.keys', 'from_table',   'char', '*', 'referencing table',     0, 1, 2),
    ('tap_schema.keys', 'target_table', 'char', '*', 'referenced table',      0, 1, 3),
    ('tap_schema.keys', 'utype',        'char', '*', 'utype of the key',      0, 1, 4),
    ('tap_schema.keys', 'description',  'char', '*', 'brief description',     0, 1, 5),
    ('tap_schema.key_columns', 'key_id',        'char', '*', 'key this column belongs to', 1, 1, 1),
    ('tap_schema.key_columns', 'from_column',   'char', '*', 'referencing column',         0, 1, 2),
    ('tap_schema.key_columns', 'target_column', 'char', '*', 'referenced column',          0, 1, 3);

-- TAP_SCHEMA describes its own relationships (TAP 1.1 sec 4; taplint TSLN):
-- a client can join the metadata tables and deserves to be told how.
INSERT INTO tap_schema.keys (key_id, from_table, target_table, description) VALUES
    ('tap_schema:tables_schema',      'tap_schema.tables',      'tap_schema.schemas', 'table membership'),
    ('tap_schema:columns_table',      'tap_schema.columns',     'tap_schema.tables',  'column membership'),
    ('tap_schema:keys_from_table',    'tap_schema.keys',        'tap_schema.tables',  'referencing table'),
    ('tap_schema:keys_target_table',  'tap_schema.keys',        'tap_schema.tables',  'referenced table'),
    ('tap_schema:key_columns_key',    'tap_schema.key_columns', 'tap_schema.keys',    'key membership');

INSERT INTO tap_schema.key_columns (key_id, from_column, target_column) VALUES
    ('tap_schema:tables_schema',     'schema_name',  'schema_name'),
    ('tap_schema:columns_table',     'table_name',   'table_name'),
    ('tap_schema:keys_from_table',   'from_table',   'table_name'),
    ('tap_schema:keys_target_table', 'target_table', 'table_name'),
    ('tap_schema:key_columns_key',   'key_id',       'key_id');
