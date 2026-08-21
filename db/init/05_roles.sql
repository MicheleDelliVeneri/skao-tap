-- Read-only role used to execute user ADQL queries; the services connect as
-- the owning user but downgrade with SET ROLE before running untrusted SQL.
CREATE ROLE tap_reader NOLOGIN;
GRANT USAGE ON SCHEMA tap_schema, ska TO tap_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA tap_schema, ska TO tap_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA tap_schema GRANT SELECT ON TABLES TO tap_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA ska GRANT SELECT ON TABLES TO tap_reader;
GRANT tap_reader TO tap;
