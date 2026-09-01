#!/bin/bash
# Start PostgreSQL, initialise DaCHS once, ingest whatever RDs are mounted
# under /var/gavo/inputs, and serve. Ingest markers live in the (persistent)
# gavo state dir so a restarted container does not re-import.
set -euo pipefail

service postgresql start

# Serve on all interfaces (the default binds 127.0.0.1, unreachable through
# a mapped port), and advertise the URL clients actually use: DaCHS writes
# its serverURL into capabilities and UWS job documents, and taplint *follows*
# those — a container that advertises its internal port sends every validator
# and async client to the wrong server. DaCHS reads /etc/gavo.rc.
if ! grep -qs '^bindAddress' /etc/gavo.rc 2>/dev/null; then
    {
        printf '[web]\nbindAddress: 0.0.0.0\n'
        printf 'serverURL: %s\n' "${DACHS_SERVER_URL:-http://localhost:8080}"
    } >> /etc/gavo.rc
fi

if [ ! -e /var/gavo/etc/dsn ]; then
    dachs init
fi

# The obscore infrastructure (ivoa._obscoresources and the view machinery)
# must exist before any table publishes into it.
if [ ! -e /var/gavo/state/obscore-ready ]; then
    dachs imp //obscore
    touch /var/gavo/state/obscore-ready
fi

# Import every mounted resource descriptor exactly once, then rebuild the
# ivoa.obscore view over the new contribution.
for rd in /var/gavo/inputs/*/q.rd; do
    [ -e "$rd" ] || continue
    resource="$(basename "$(dirname "$rd")")"
    marker="/var/gavo/state/imported-${resource}"
    if [ ! -e "$marker" ]; then
        dachs imp "${resource}/q"
        dachs imp //obscore
        dachs limits "${resource}/q" || true
        touch "$marker"
    fi
done

exec dachs serve debug
