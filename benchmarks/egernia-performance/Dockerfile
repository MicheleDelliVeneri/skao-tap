# The dataset seeder: the benchmark suite's generator, packaged to run as a
# deployment's post-deploy Job.
#
# Deliberately not the tap-api image. That one installs the service and none of
# the suite, and adding the suite to it would put a data generator into the
# image that serves production traffic.
#
# Deliberately not the whole `benchmark` dependency group either: generation
# needs psycopg and a YAML parser, while the group also carries matplotlib,
# numpy, pyarrow, psutil and jinja2 for analysis and reporting that a seeding
# Job never does.
FROM python:3.14-slim

# psycopg[binary] ships its own libpq, so no system postgresql-client is
# needed. Versions match the `benchmark` group in the repository's
# pyproject.toml, so the seeder and a benchmark run drive identical code.
RUN pip install --no-cache-dir "psycopg[binary]>=3.2" "pyyaml>=6.0"

WORKDIR /suite

# Only what generation reads: the dataset subpackage (its two .sql files
# included) and the tier/knob config.
COPY egernia_bench/__init__.py ./egernia_bench/__init__.py
COPY egernia_bench/dataset ./egernia_bench/dataset
COPY config/datasets.yaml ./config/datasets.yaml

ENV PYTHONPATH=/suite PYTHONUNBUFFERED=1

# Unprivileged: the Job only needs a TCP connection to PostgreSQL and a
# writable /tmp for its checkpoint markers.
RUN useradd --uid 10001 --create-home seeder
USER 10001

ENTRYPOINT ["python", "-m", "egernia_bench.dataset.seed"]
