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
RUN groupadd -g 10001 tap && useradd -m -u 10001 -g tap tap \
    && mkdir -p /results && chown tap:tap /results
COPY --from=builder /srv/.venv /srv/.venv
# astropy and friends cache under HOME; keep it on tmpfs-friendly paths so
# the container can run with a read-only root filesystem
ENV HOME=/tmp XDG_CACHE_HOME=/tmp/.cache XDG_CONFIG_HOME=/tmp/.config
USER 10001
EXPOSE 8080
CMD ["/srv/.venv/bin/uvicorn", "tap_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
