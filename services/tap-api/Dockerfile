FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /usr/local/bin/uv
# git is needed to fetch the ska-src-mm-notification dependency from GitLab
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY pyproject.toml uv.lock ./
COPY libs/tapcore ./libs/tapcore
COPY services/tap-api ./services/tap-api
COPY services/tap-executor/pyproject.toml ./services/tap-executor/pyproject.toml
RUN mkdir -p services/tap-executor/tap_executor \
    && touch services/tap-executor/tap_executor/__init__.py \
    && uv sync --frozen --no-dev --no-editable --package tap-api

FROM python:3.12-slim
COPY --from=builder /srv/.venv /srv/.venv
EXPOSE 8080
CMD ["/srv/.venv/bin/uvicorn", "tap_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
