# TAP metadata demo

`srcnet_metadata_tap.ipynb` populates 100 positioned data products and a
software document through the model-backed JSON ingest API. It demonstrates
OpenAPI, VOSI, JSON and `TAP_SCHEMA` discovery; synchronous JSON and PyVO
queries; spatial ADQL; artifact joins; asynchronous TAP and JSON job
lifecycles; validation errors; nested fetch/list; validated amendment; and
cascading deletion.

Start the service and notebook from the repository root:

```bash
docker compose up --build -d
uv run --group dev --with jupyter jupyter lab demo/srcnet_metadata_tap.ipynb
```

The notebook defaults to `http://localhost:8080/tap`. Set the `TAP_URL`
environment variable before starting Jupyter to target another deployment.
Re-running it is safe: both ingest endpoints upsert by model identity. The
run leaves the ingested project and software record in place for further
exploration; the last cell removes them, and every row they cascade to, once
`CLEANUP` is set to `True`.

## `scaling_demo.py` in the SRCNet notebook

The deployment stack's `sync-notebooks` copies this directory into the
JupyterHub image's shared volume, where it appears as
`work/demo/egernia/`. That route does **not** install this repository, so the
demo arrives with only whatever the notebook image happens to carry — which is
why the `demo` dependency group has to name everything the notebook needs
outright, including `pyarrow`, which otherwise only reaches a local checkout as
a dependency of `egernia-core`.

Measured on the dev cluster's notebook, in case the next thing to break is the
environment rather than the code:

| | |
| --- | --- |
| present | `marimo`, `httpx`, `numpy`, `pyvo` |
| missing, install with `pip install --user` | `altair`, `pandas`, `pyarrow` |
| absent entirely | `fire`, `selenium`, `fastapi`, and any browser |

The last row is why the token comes from the device flow with a human approving
it rather than from `ska_src_auth_api.client.integration`, which drives Chrome
through the IAM form: there is no Chrome, and not even the base client imports.

Two environment settings that route needs:

- `EGERNIA_BASE_URL=http://egernia.test`, or `EGERNIA_INSECURE_TLS=1` to keep
  https. egernia's dev ingress has no `tls:` section, so nginx answers 443 with
  its own self-signed certificate and httpx refuses it. AAPI and IAM are behind
  the cluster CA and verify normally, so `EGERNIA_AAPI_INSECURE_TLS` is
  separate and should stay off — it governs the leg that carries the token.
- `EGERNIA_TOKEN`, if nobody is available to approve a device login.
