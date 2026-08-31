# GAVO DaCHS, installed the way its own documentation installs it: the
# gavodachs2-server Debian package (DaCHS is part of Debian since bullseye).
# One container carries DaCHS and its PostgreSQL, which is the layout the
# package configures out of the box — the vendor-documented deployment this
# benchmark promises to measure, not a layout we invented.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
# The package's postinst configures DaCHS against a running PostgreSQL, so
# the cluster must be started inside the same build step.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql curl \
    && service postgresql start \
    && apt-get install -y --no-install-recommends gavodachs2-server \
    && service postgresql stop \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
