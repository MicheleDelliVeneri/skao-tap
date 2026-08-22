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
Re-running it is safe: both ingest endpoints upsert by model identity.
