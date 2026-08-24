# Contributing

Thanks for considering a contribution. Bug reports, standards-conformance
findings and performance measurements are all as welcome as code.

## Where to ask

- **Questions and bug reports** — [GitHub issues](https://github.com/MicheleDelliVeneri/skao-tap/issues).
  For a conformance bug, the most useful report names the standard and section
  the service is getting wrong, and the client that noticed.
- **Security vulnerabilities** — follow [SECURITY.md](SECURITY.md). Please do
  not open a public issue.

## Development loop

The repo is a [uv](https://docs.astral.sh/uv/) workspace (`libs/tapcore`,
`services/tap-api`, `services/tap-executor`) on Python 3.14+:

```bash
uv sync --all-groups                 # dev + docs groups
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/unit             # tapcore / tap-api unit tests
uv run pytest tests/component        # boots the stack, exercised with PyVO
uv run --group docs mkdocs serve     # docs at http://localhost:8000
```

The component tests need a reachable PostgreSQL and skip without one — see the
[development guide](https://micheledelliveneri.github.io/skao-tap/development/)
for the connection details and the Docker Compose shortcut.

## What CI expects

`.github/workflows/ci.yml` runs lint, unit tests, component tests, a docs
build and Helm checks on every push and pull request. A change is ready when
those pass locally:

- **Lint and format** clean under `ruff` — the configuration in
  `pyproject.toml` is the whole style guide, so there is nothing to argue
  about by hand.
- **Tests alongside the change.** Behaviour that a VO client can observe
  belongs in `tests/component` against PyVO, because that is what proves the
  service does what the standard says rather than what we assumed.
- **Docs updated in the same PR** when behaviour changes. The `docs/` tree is
  the reference; the README is deliberately a signpost, so most changes land
  in `docs/` only.
- **`mkdocs build --strict` passes**, which catches broken internal links.

## Pull requests

Branch off `main`, keep the change focused, and describe what a reviewer
should look at rather than restating the diff. Conventional-commit style
subjects (`fix:`, `feat:`, `docs:`) are used throughout the history.

If a change alters the standards surface — a new endpoint, parameter, output
format or capability — say which IVOA document and section it implements, and
update `/capabilities` so clients can discover it.

## Roadmap

Larger work is organised as numbered packages in the
[roadmap](https://micheledelliveneri.github.io/skao-tap/roadmap/), referenced
by number in issues and PRs. If you plan something substantial, opening an
issue first saves duplicated effort.

By contributing you agree that your work is licensed under the repository's
[MIT licence](LICENSE), and you are expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
