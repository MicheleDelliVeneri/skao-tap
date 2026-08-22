-- One-off migration for deployments upgraded across the software-domain move
-- into the shared srcnet schema:
--
--     software.software  -> srcnet.software
--     software.artifacts -> srcnet.software_artifacts
--
-- The service's generated DDL is additive (CREATE TABLE IF NOT EXISTS,
-- ADD COLUMN IF NOT EXISTS), so it can migrate a model that grew fields but
-- not a domain that moved: the old tables, their TAP_SCHEMA registration and
-- their read grant all survive the upgrade. Rows left there are invisible to
-- the JSON API (ingest, fetch, list, amend) and are NOT removed by
-- DELETE /api/v1/software/{uri}, yet stay queryable over TAP — a deleted
-- document appears to come back. Startup logs a warning while that is true.
--
-- This script carries the rows forward and then drops the legacy schema.
-- It deletes data, so run it deliberately (psql -1 -f), with a backup:
--
--     docker compose exec -T postgres psql -U postgres -d tap -1 \
--         -f - < scripts/migrate_legacy_tables.sql
--
-- It is idempotent and a no-op on deployments that never had the old layout.

BEGIN;

-- 1. Carry rows forward, matching on column name so a legacy table created by
--    an older model release (different column order, extra dropped columns)
--    still migrates. Parents first: the FK cascade requires the root row.
DO $$
DECLARE
    move CONSTANT text[][] := ARRAY[
        ARRAY['software', 'software', 'srcnet', 'software'],
        ARRAY['software', 'artifacts', 'srcnet', 'software_artifacts']
    ];
    step text[];
    cols text;
BEGIN
    FOREACH step SLICE 1 IN ARRAY move LOOP
        IF to_regclass(format('%I.%I', step[1], step[2])) IS NULL
           OR to_regclass(format('%I.%I', step[3], step[4])) IS NULL THEN
            CONTINUE;
        END IF;
        SELECT string_agg(quote_ident(old.column_name), ', ')
          INTO cols
          FROM information_schema.columns old
         WHERE old.table_schema = step[1]
           AND old.table_name = step[2]
           AND EXISTS (
               SELECT 1
                 FROM information_schema.columns new
                WHERE new.table_schema = step[3]
                  AND new.table_name = step[4]
                  AND new.column_name = old.column_name
           );
        IF cols IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format(
            'INSERT INTO %I.%I (%s) SELECT %s FROM %I.%I ON CONFLICT DO NOTHING',
            step[3], step[4], cols, cols, step[1], step[2]
        );
    END LOOP;
END $$;

-- 2. Unregister the legacy tables from TAP_SCHEMA (children of the FK chain
--    first) so /tables and ADQL stop advertising them.
DELETE FROM tap_schema.key_columns
 WHERE key_id IN (
     SELECT key_id FROM tap_schema.keys
      WHERE from_table LIKE 'software.%' OR target_table LIKE 'software.%'
 );
DELETE FROM tap_schema.keys
 WHERE from_table LIKE 'software.%' OR target_table LIKE 'software.%';
DELETE FROM tap_schema.columns WHERE table_name LIKE 'software.%';
DELETE FROM tap_schema.tables WHERE table_name LIKE 'software.%';
DELETE FROM tap_schema.schemas WHERE schema_name = 'software';

-- 3. Drop the legacy tables themselves (and the read grant that went with
--    the schema).
DROP SCHEMA IF EXISTS software CASCADE;

COMMIT;
