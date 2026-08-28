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
JupyterHub image's shared volume, where it appears as `work/demo/egernia/`.
That route does **not** install this repository, so the demo arrives with only
whatever the singleuser image happens to carry.

`requirements.txt` beside this file is the manifest for that case — the only
one that travels with the copied folder. It is kept in lockstep with the `demo`
dependency group in `pyproject.toml` by
`tests/unit/test_demo_requirements.py`, because the two lists are the same list
in two formats and drift between them surfaces as a missing module in someone
else's notebook rather than as a failure here.

Measured on the dev cluster's notebook image, in case the next thing to break
is the environment rather than the code:

| | |
| --- | --- |
| in the image | `marimo`, `httpx`, `numpy`, `pyvo`, `matplotlib`, `astropy`, `fastapi`, `fire` |
| **not** in the image | `altair`, `pandas`, `pyarrow` — hence `requirements.txt` |
| absent entirely | `selenium`, and any browser |

Do not reach for `pip install --user` to fix the middle row. It appears to work
and does not survive: the singleuser PVC is recreated with the server (observed
— claim created `15:06:58`, pod started `15:07:01`), which takes `~/.local`
with it. The packages have to come from the image or from an install step that
runs on every pod start.

The last row is why `auth.py` runs the device flow itself instead of using
`ska_src_auth_api.client.integration`, which drives Chrome through the IAM form
with Selenium: there is no Chrome. The base `AuthenticationClient` does import
and does expose the three calls needed, so that would also be possible; doing
it directly avoids depending on the auth API's source tree being mounted on
`PYTHONPATH`, and on client errors arriving as `fastapi.HTTPException`.

Two environment settings that route needs:

- `EGERNIA_BASE_URL=http://egernia.test`, or `EGERNIA_INSECURE_TLS=1` to keep
  https. egernia's dev ingress has no `tls:` section, so nginx answers 443 with
  its own self-signed certificate and httpx refuses it. AAPI and IAM are behind
  the cluster CA and verify normally, so `EGERNIA_AAPI_INSECURE_TLS` is
  separate and should stay off — it governs the leg that carries the token.
- `EGERNIA_TOKEN`, if nobody is available to approve a device login.
