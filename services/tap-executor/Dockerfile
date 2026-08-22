FROM python:3.14-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /srv
COPY pyproject.toml uv.lock ./
COPY libs/tapcore ./libs/tapcore
COPY services/tap-executor ./services/tap-executor
COPY services/tap-api/pyproject.toml ./services/tap-api/pyproject.toml
RUN mkdir -p services/tap-api/tap_api \
    && touch services/tap-api/tap_api/__init__.py \
    && uv sync --frozen --no-dev --no-editable --package tap-executor \
    && groupadd -g 10001 tap && useradd -m -u 10001 -g tap tap \
    && mkdir -p /results && chown tap:tap /results
ENV HOME=/tmp XDG_CACHE_HOME=/tmp/.cache XDG_CONFIG_HOME=/tmp/.config
USER 10001
CMD ["/srv/.venv/bin/python", "-m", "tap_executor.worker"]
