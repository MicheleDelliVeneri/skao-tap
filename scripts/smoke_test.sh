#!/usr/bin/env bash
# End-to-end smoke test against a running docker-compose stack.
set -euo pipefail
BASE=${1:-http://localhost:8080/tap}

echo "== availability ==";  curl -fsS "$BASE/availability" | head -5
echo "== capabilities =="; curl -fsS "$BASE/capabilities" | head -8
echo "== tables ==";       curl -fsS "$BASE/tables" | head -12

echo "== sync ADQL query =="
curl -fsS "$BASE/sync" \
    --data-urlencode "LANG=ADQL" \
    --data-urlencode "QUERY=SELECT TOP 3 source_name, ra, dec, flux_int FROM ska.continuum_sources ORDER BY flux_int DESC" \
    --data-urlencode "RESPONSEFORMAT=csv"

echo "== sync TAP_SCHEMA query =="
curl -fsS "$BASE/sync" \
    --data-urlencode "LANG=ADQL" \
    --data-urlencode "QUERY=SELECT table_name FROM tap_schema.tables" \
    --data-urlencode "RESPONSEFORMAT=csv"

echo "== async job =="
JOB_URL=$(curl -fsS -o /dev/null -w '%{redirect_url}' "$BASE/async" \
    --data-urlencode "LANG=ADQL" \
    --data-urlencode "QUERY=SELECT source_id, source_name FROM ska.continuum_sources" \
    --data-urlencode "PHASE=RUN")
echo "job: $JOB_URL"
for _ in $(seq 1 30); do
    PHASE=$(curl -fsS "$JOB_URL/phase")
    echo "phase: $PHASE"
    [ "$PHASE" = COMPLETED ] && break
    [ "$PHASE" = ERROR ] && { curl -fsS "$JOB_URL/error"; exit 1; }
    sleep 1
done
echo "== result =="
curl -fsS "$JOB_URL/results/result" | head -40
echo "smoke test passed"
