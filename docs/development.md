# Development

The repository is a [uv](https://docs.astral.sh/uv/) workspace with three
members: `libs/tapcore`, `services/tap-api`, `services/tap-executor`.

```bash
uv sync --all-groups        # create .venv with all members + dev/docs groups
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

The **unit tests** cover `tapcore` in isolation: ADQL translation, result
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
automatically when no server is reachable. Local one-time setup mirrors CI:

```bash
sudo apt-get install postgresql postgresql-16-pgsphere
sudo service postgresql start
sudo -u postgres psql -c "CREATE USER tap WITH PASSWORD 'tap' SUPERUSER" \
                      -c "CREATE DATABASE tap OWNER tap"
```

## Docs

```bash
uv run --group docs mkdocs serve   # live preview
uv run --group docs mkdocs build --strict
```

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
