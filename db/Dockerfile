# PostgreSQL with the pg_sphere extension (spherical geometry used by
# translated ADQL POINT/CIRCLE/CONTAINS expressions). The official postgres
# image ships the PGDG apt repository, so the pgsphere package is available.
FROM postgres:16
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgsphere \
    && rm -rf /var/lib/apt/lists/*

# Run as the postgres user (uid 999) instead of root; the official image
# supports this when the data directory is writable by that user.
USER postgres
