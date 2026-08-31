FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 /uv /usr/local/bin/uv
# git fetches ska-src-logging from GitLab (pinned to a commit until the
# package is published to a public index), like tap-api does for ska-src-sdm.
# Split into builder and runtime stages for it, so git is not in the image
# that runs.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY pyproject.toml uv.lock ./
COPY libs/egernia-core ./libs/egernia-core
COPY services/egernia-executor ./services/egernia-executor
# the sibling's manifest only: uv sync --frozen validates every workspace
# member's pyproject, even ones outside the installed package's closure
COPY services/egernia-api/pyproject.toml ./services/egernia-api/pyproject.toml
RUN uv sync --frozen --no-dev --no-editable --package egernia-executor

FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4
RUN groupadd -g 10001 tap && useradd -m -u 10001 -g tap tap \
    && mkdir -p /results && chown tap:tap /results
COPY --from=builder /srv/.venv /srv/.venv
ENV HOME=/tmp XDG_CACHE_HOME=/tmp/.cache XDG_CONFIG_HOME=/tmp/.config
USER 10001
EXPOSE 9100
# The worker had nothing to probe until it began serving metrics; that
# listener is a fair liveness signal, since the loop starts it and holds it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD ["/srv/.venv/bin/python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/metrics' % os.environ.get('TAP_EXECUTOR_METRICS_PORT', '9100'), timeout=4)"]
CMD ["/srv/.venv/bin/python", "-m", "egernia_executor.worker"]
