FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /usr/local/bin/uv
WORKDIR /srv
COPY pyproject.toml uv.lock ./
COPY libs/tapcore ./libs/tapcore
COPY services/tap-api ./services/tap-api
COPY services/tap-executor/pyproject.toml ./services/tap-executor/pyproject.toml
RUN mkdir -p services/tap-executor/tap_executor \
    && touch services/tap-executor/tap_executor/__init__.py \
    && uv sync --frozen --no-dev --no-editable --package tap-api
EXPOSE 8080
CMD ["/srv/.venv/bin/uvicorn", "tap_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
