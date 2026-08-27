-- Spherical-geometry extension used by ADQL geometry translations
-- (POINT/CIRCLE/CONTAINS -> spoint/scircle/@ operators).
DO $do$
BEGIN
    BEGIN
        CREATE EXTENSION IF NOT EXISTS pg_sphere;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'pg_sphere not available: ADQL geometry functions will fail (%).', SQLERRM;
    END;
END
$do$;
