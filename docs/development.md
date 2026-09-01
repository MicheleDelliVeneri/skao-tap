# Development

The repository is a [uv](https://docs.astral.sh/uv/) workspace with three
members: `libs/egernia-core`, `services/egernia-api`, `services/egernia-executor`.

```bash
uv sync --all-groups        # create .venv with all members + every group (dev, docs, demo, dataset, microbenchmark)
```

## Lint & format

[ruff](https://docs.astral.sh/ruff/) is configured in the root
`pyproject.toml` (lint + format, line length 100):

```bash
uv run ruff check .          # lint
uv run ruff format --check . # formatting
```

## Tests

```bash
uv run pytest tests/unit          # fast, no external dependencies
uv run pytest tests/component    # needs PostgreSQL (+ pg_sphere) and psql
```

The **unit tests** cover `egernia_core` in isolation: ADQL translation, result
serialization, UWS XML rendering, and DALI parameter handling.

The **component tests** boot the real stack — a dedicated
`tap_component_test` database initialized from `db/init/*.sql`, a `tap-api`
uvicorn process and a `tap-executor` worker — and exercise the service the
way IVOA clients do, using **PyVO** (`TAPService`, `AsyncTAPJob`) plus raw
HTTP for protocol details: VOSI documents, sync queries (formats, `MAXREC`,
overflow, geometry, `TAP_SCHEMA`), DALI error documents, and the full UWS
job lifecycle (run, abort, delete, error jobs, parameter updates, execution
duration, destruction, job listing).

They connect using `TAP_TEST_ADMIN_URL`
(default `postgresql://tap:tap@127.0.0.1:5432/postgres`) and skip
automatically when no server is reachable. The simplest server is the
project's own database image — the same one CI runs, so the tests always see
the shipped PostgreSQL major and pg_sphere build:

```bash
docker build -t egernia/tap-db db
docker run -d --name tap-db -p 5432:5432 \
  -e POSTGRES_USER=tap -e POSTGRES_PASSWORD=tap -e POSTGRES_DB=tap \
  egernia/tap-db
docker exec tap-db psql -U tap -d tap -c "ALTER USER tap WITH SUPERUSER"
```

The fixtures shell out to `psql` to load `db/init/*.sql`, so a client is
needed on the host too (`apt-get install postgresql-client`, or
`brew install libpq`). A locally installed server works as well, provided it
is PostgreSQL 18 with `postgresql-18-pgsphere` from the
[PGDG repository](https://www.postgresql.org/download/linux/).

## Docs

```bash
uv run --group docs mkdocs serve   # live preview
uv run --group docs mkdocs build --strict
```

Every MkDocs build runs `scripts/generate_model_schema_docs.py` as a hook.
It loads the installed metadata plugins and regenerates
`docs/model-schemas.md` directly from their pydantic models, keeping the
published table/column reference aligned with the service's generated SQL
and `TAP_SCHEMA` metadata. Run the script directly to refresh the checked-in
page without building the full site.

The PyVO notebook in `demo/srcnet_metadata_tap.ipynb` can be launched with:

```bash
docker compose up --build -d
uv run --group dev --with jupyter jupyter lab demo/srcnet_metadata_tap.ipynb
```

## Upgrading an existing deployment

The generated DDL migrates a database forward automatically as long as the
change is additive — a newer model release that adds fields gets
`ADD COLUMN IF NOT EXISTS`, existing rows are untouched. **Renames are not
automatic**: additive DDL cannot move rows, so a metadata domain that moves
to another schema or table name leaves the old tables, their `TAP_SCHEMA`
registration and their read grant in place.

That matters beyond tidiness: rows stranded in a pre-rename table are
invisible to the JSON API (ingest, fetch, list, amend) and are *not* removed
by `DELETE /api/v1/<mount>/{root_id}`, yet they stay queryable through TAP —
so a deleted document appears to come back. Startup logs a warning for every
legacy table it still finds (declared per plugin as `legacy_tables`).

The `software` domain moved into the shared `srcnet` schema
(`software.software` → `srcnet.software`, `software.artifacts` →
`srcnet.software_artifacts`). Deployments created before that move should run
the one-off migration, which carries the rows forward (matching on column
name, so an older column order still migrates), unregisters the legacy tables
from `TAP_SCHEMA` and drops the old schema:

```bash
docker compose exec -T db psql -U tap -d tap -v ON_ERROR_STOP=1 \
    -f - < scripts/migrate_legacy_tables.sql
```

It deletes data, so take a backup first. It is idempotent, and a no-op on
databases that never had the old layout.

## CI/CD

GitHub Actions (`.github/workflows/ci.yml`) runs on every push/PR:

1. **lint** — ruff check + format check
2. **unit** — `pytest tests/unit`
3. **component** — PostgreSQL + pg_sphere on the runner, `pytest tests/component`
4. **helm** — `helm lint` + `helm template`, and checks the chart's copy of
   the DB init SQL matches `db/init/`
5. **docs** — `mkdocs build --strict`
6. **images** — Docker builds of all three images; pushed to GHCR on `main`

`docs.yml` publishes the documentation to GitHub Pages on pushes to `main`.
