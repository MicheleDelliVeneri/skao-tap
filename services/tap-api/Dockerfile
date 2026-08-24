FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 /uv /usr/local/bin/uv
# git fetches the ska-src-sdm dependency from GitLab (pinned to a commit
# until the package is published to a public index)
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

FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4
RUN groupadd -g 10001 tap && useradd -m -u 10001 -g tap tap \
    && mkdir -p /results && chown tap:tap /results
COPY --from=builder /srv/.venv /srv/.venv
# astropy and friends cache under HOME; keep it on tmpfs-friendly paths so
# the container can run with a read-only root filesystem
ENV HOME=/tmp XDG_CACHE_HOME=/tmp/.cache XDG_CONFIG_HOME=/tmp/.config
USER 10001
# The default only — EXPOSE is fixed at build time, so a container started
# with TAP_API_PORT set listens elsewhere and that port has to be published
# explicitly rather than by docker run -P.
EXPOSE 8080
# Follows TAP_API_PORT like the command below, or the health status would
# describe a port the service is not listening on.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["/srv/.venv/bin/python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/tap/availability' % os.environ.get('TAP_API_PORT', '8080'), timeout=4)"]
# Run through a shell so the worker count and port come from the environment:
# ADQL translation is pure-Python ANTLR and holds the GIL, so one process
# serves one query at a time however many CPUs the pod has, and
# TAP_API_WORKERS is what lets a deployment use them. exec keeps uvicorn in
# place for signal handling; the expansions are quoted so a malformed value
# cannot become extra shell words.
# TAP_API_BACKLOG sizes the listen socket's accept queue (uvicorn's default
# is 2048); when it overflows, the kernel drops or resets connections before
# the application ever sees them — the client reads a reset, not a refusal.
# TAP_API_LIMIT_CONCURRENCY (per worker process; unset means unlimited) is
# the application-level counterpart: past that many concurrent connections
# uvicorn answers 503 immediately, so overload sheds with a refusal a client
# can back off from instead of a reset it can only retry blind.
CMD ["/bin/sh", "-c", "exec /srv/.venv/bin/uvicorn tap_api.main:app --host 0.0.0.0 --port \"${TAP_API_PORT:-8080}\" --workers \"${TAP_API_WORKERS:-1}\" --backlog \"${TAP_API_BACKLOG:-2048}\" ${TAP_API_LIMIT_CONCURRENCY:+--limit-concurrency \"${TAP_API_LIMIT_CONCURRENCY}\"}"]
