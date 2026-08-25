-- Read-only role used to execute user ADQL queries; the services connect as
-- the owning user but downgrade with SET ROLE before running untrusted SQL.
-- Idempotent so the scripts can be replayed (e.g. by the component tests).
DO $do$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'tap_reader') THEN
        CREATE ROLE tap_reader NOLOGIN;
    END IF;
END
$do$;
GRANT USAGE ON SCHEMA tap_schema, ska TO tap_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA tap_schema, ska TO tap_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA tap_schema GRANT SELECT ON TABLES TO tap_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA ska GRANT SELECT ON TABLES TO tap_reader;
DO $do$
BEGIN
    EXECUTE format('GRANT tap_reader TO %I', current_user);
END
$do$;
