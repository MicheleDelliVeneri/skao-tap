# The dataset seeder, packaged to run as a deployment's post-deploy Job.
#
# Deliberately not the tap-api image: adding a data generator to the image that
# serves traffic would ship a way to write 500,000 rows into production.
#
# Two libraries rather than a dependency group. Generation needs psycopg and a
# YAML parser; nothing here analyses or reports.
FROM python:3.14-slim

# psycopg[binary] ships its own libpq, so no system postgresql-client is needed.
RUN pip install --no-cache-dir "psycopg[binary]>=3.2" "pyyaml>=6.0"

WORKDIR /srv

# Only what generation reads: the package, its two .sql files included, and the
# generation knobs.
COPY egernia_dataset ./egernia_dataset
COPY config/datasets.yaml ./config/datasets.yaml

ENV PYTHONPATH=/srv PYTHONUNBUFFERED=1

# Unprivileged: the Job needs a TCP connection to PostgreSQL and a writable
# /tmp for its checkpoint markers, and nothing else.
RUN useradd --uid 10001 --create-home seeder
USER 10001

ENTRYPOINT ["python", "-m", "egernia_dataset.seed"]
