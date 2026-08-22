# PostgreSQL with the pg_sphere extension (spherical geometry used by
# translated ADQL POINT/CIRCLE/CONTAINS expressions). The official postgres
# image ships the PGDG apt repository, so the pgsphere package is available.
#
# The major version appears twice — the base image and the versioned pgsphere
# package — and both must move together: postgresql-<N>-pgsphere only builds
# against server major N.
FROM postgres:18
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-18-pgsphere \
    && rm -rf /var/lib/apt/lists/*

# postgres:18 keeps its data under /var/lib/postgresql/<major>/docker and no
# longer ships /var/lib/postgresql/data. Compose and the Helm chart both put
# PGDATA at /var/lib/postgresql/data/pgdata, so create that mount point with
# postgres ownership: a fresh Docker named volume inherits the directory's
# permissions from the image, and without this the non-root server cannot
# create its data directory.
RUN mkdir -p /var/lib/postgresql/data     && chown postgres:postgres /var/lib/postgresql/data     && chmod 700 /var/lib/postgresql/data

# Run as the postgres user (uid 999) instead of root; the official image
# supports this when the data directory is writable by that user.
USER postgres
