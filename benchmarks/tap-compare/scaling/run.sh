#!/usr/bin/env bash
# The resource-scaling comparison driver (PROTOCOL.md): for each tier, bring
# both stacks up under the tier's pins, run the gates with both up, then
# measure the servers one at a time (the other stopped) into ONE run
# directory, recording what `docker inspect` and `SHOW` actually saw.
#
#   nohup setsid benchmarks/tap-compare/scaling/run.sh > scaling.log 2>&1 &
#   grep PROGRESS scaling.log
#
# Environment: SCENARIO (scaling | scaling-smoke), TIERS ("8 16 24"),
# GEN_CPUS (taskset list for the generator, "24-29"), RUN_NAME (an existing
# run directory to resume), RESTORE (1: end with both stacks back on the
# parity pins). Requires the seeded egernia volume and DaCHS's ingested data.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SUITE=$(dirname "$HERE")
REPO=$(cd "$SUITE/../.." && pwd)
SCENARIO=${SCENARIO:-scaling}
TIERS=${TIERS:-8 16 24}
GEN_CPUS=${GEN_CPUS:-24-29}
RUN_NAME=${RUN_NAME:-}
RESTORE=${RESTORE:-1}
TARGETS="egernia-local dachs-local"
EGERNIA_URL=http://localhost:8080/tap
DACHS_URL=http://localhost:8081/tap
EXPECTED_ROWS=500096
EXPECTED_TRGM_INDEXES=1
EXPECTED_FKS=16

log() { echo "$(date -u +%FT%TZ) $*"; }
fail() { log "FAIL $*"; exit 1; }
expect() { [ "$2" = "$3" ] || fail "$1: got '$2', expected '$3'"; }

tap() {
    (cd "$REPO" && taskset -c "$GEN_CPUS" uv run --group tap-compare \
        python benchmarks/tap-compare --config-dir "$HERE" "$@")
}
compose_egernia() {
    docker compose -f "$REPO/docker-compose.yml" -f "$HERE/pins/egernia-$1.yml" "${@:2}"
}
compose_dachs() {
    docker compose -f "$SUITE/docker-compose.dachs.yml" -f "$HERE/pins/dachs-$1.yml" "${@:2}"
}
pg_egernia() { docker exec egernia-db-1 psql -U tap -d tap -Atc "$1"; }
pg_dachs() { docker exec tap-compare-dachs-1 su postgres -c "psql -Atc \"$1\" gavo"; }
pin() { grep -o "$2=[^ ]*" "$HERE/pins/egernia-$1.yml" | head -1 | cut -d= -f2; }
SHOW_SETTINGS="select name || ' = ' || current_setting(name) from pg_settings where name in \
    ('shared_buffers', 'effective_cache_size', 'work_mem', 'max_parallel_workers', \
    'max_worker_processes', 'max_connections')"

wait_tap() {
    for _ in $(seq 1 120); do
        curl -fsS -o /dev/null "$1/capabilities" && return 0
        sleep 5
    done
    fail "$1 did not answer within 10 minutes"
}

# up <server> <tier>: (re)create under the tier's pins, wait, verify the data
# and the settings the pins promised.
up() {
    local server=$1 tier=$2
    log "PROGRESS tier=$tier server=$server phase=up"
    if [ "$server" = egernia ]; then
        compose_egernia "$tier" up -d --no-build --force-recreate db tap-api tap-executor
        wait_tap $EGERNIA_URL
        expect "egernia rows" "$(pg_egernia 'select count(*) from ivoa.obscore')" $EXPECTED_ROWS
        expect "egernia trigram index" "$(pg_egernia "select count(*) from pg_indexes \
            where indexname = 'data_products_obscore_did_trgm'")" $EXPECTED_TRGM_INDEXES
        expect "egernia foreign keys" "$(pg_egernia "select count(*) from pg_constraint \
            where contype = 'f' and connamespace = 'srcnet'::regnamespace")" $EXPECTED_FKS
        for setting in shared_buffers effective_cache_size max_parallel_workers \
            max_worker_processes; do
            expect "egernia $setting" "$(pg_egernia "show $setting")" "$(pin "$tier" $setting)"
        done
        local workers
        workers=$(grep -o 'TAP_API_WORKERS: "[0-9]*"' "$HERE/pins/egernia-$tier.yml" | grep -o '[0-9]*')
        # one process for a single worker; a supervisor plus one per worker above that
        local processes minimum
        processes=$(docker top egernia-tap-api-1 -o pid,args | grep -c '/srv/.venv/bin/python')
        minimum=$([ "$workers" = 1 ] && echo 1 || echo $((workers + 1)))
        [ "$processes" -ge "$minimum" ] || fail "egernia api processes: $processes < $minimum"
    else
        compose_dachs "$tier" up -d --no-build --force-recreate
        wait_tap $DACHS_URL
        expect "dachs rows" "$(pg_dachs 'select count(*) from ivoa.obscore')" $EXPECTED_ROWS
        for setting in shared_buffers effective_cache_size max_parallel_workers \
            max_worker_processes; do
            expect "dachs $setting" "$(pg_dachs "show $setting")" \
                "$(awk -v s=$setting '$1 == s {print $3}' "$HERE/pins/dachs-postgres-$tier.conf")"
        done
    fi
}

down() {
    log "PROGRESS tier=$2 server=$1 phase=down"
    if [ "$1" = egernia ]; then
        compose_egernia "$2" stop db tap-api tap-executor
    else
        compose_dachs "$2" stop
    fi
}

# record <server> <tier>: the pins as applied, into the run directory.
record() {
    local server=$1 tier=$2 dir="$SUITE/results/$RUN_NAME/pins" containers
    mkdir -p "$dir"
    if [ "$server" = egernia ]; then
        containers="egernia-db-1 egernia-tap-api-1 egernia-tap-executor-1"
    else
        containers="tap-compare-dachs-1"
    fi
    # shellcheck disable=SC2086
    docker inspect $containers --format '{{json .HostConfig}}' | python3 -c '
import json, sys
print(json.dumps([{k: h[k] for k in ("CpusetCpus", "NanoCpus", "Memory")}
                  for h in map(json.loads, sys.stdin)], indent=2))' > "$dir/t$tier-$server-local.json"
    {
        echo "# $(date -u +%FT%TZ) tier $tier $server, order: $ORDER"
        for c in $containers; do
            echo "## $c"
            docker inspect "$c" --format 'cpuset={{.HostConfig.CpusetCpus}} nanocpus={{.HostConfig.NanoCpus}} memory={{.HostConfig.Memory}}'
            docker inspect "$c" --format '{{join .Config.Cmd " "}}'
            docker inspect "$c" --format '{{join .Config.Env "\n"}}' | grep -E '^TAP_API_WORKERS=' || true
            docker top "$c" -o pid,args | sed 's/^/  /'
        done
        echo "## SHOW"
        if [ "$server" = egernia ]; then
            pg_egernia "$SHOW_SETTINGS"
        else
            pg_dachs "$SHOW_SETTINGS"
        fi
    } > "$dir/t$tier-$server-local.txt"
}

measure() {
    local server=$1 tier=$2
    log "PROGRESS tier=$tier server=$server phase=warm"
    tap run --target "$server-local" --scenario warm
    log "PROGRESS tier=$tier server=$server phase=measure"
    tap compare --targets $TARGETS --scenario "$SCENARIO" --tier "$tier" \
        --only "$server-local" --resume "$RUN_NAME"
    log "PROGRESS tier=$tier server=$server phase=done"
}

log "PROGRESS start scenario=$SCENARIO tiers='$TIERS' generator_cpus=$GEN_CPUS"
i=0
for tier in $TIERS; do
    # alternate which server goes first: egernia, dachs, egernia, ...
    if [ $((i % 2)) = 0 ]; then first=egernia second=dachs; else first=dachs second=egernia; fi
    ORDER="$first then $second"
    i=$((i + 1))
    up egernia "$tier"
    up dachs "$tier"
    log "PROGRESS tier=$tier phase=gates"
    if [ -z "$RUN_NAME" ]; then
        tap compare --targets $TARGETS --scenario "$SCENARIO" --tier "$tier" --gates-only
        RUN_NAME=$(basename "$(ls -td "$SUITE"/results/*-tap-compare-scaling | head -1)")
        log "PROGRESS run=$RUN_NAME"
    else
        tap compare --targets $TARGETS --scenario "$SCENARIO" --tier "$tier" --gates-only \
            --resume "$RUN_NAME"
    fi
    record egernia "$tier"
    record dachs "$tier"
    down $second "$tier"
    measure $first "$tier"
    down $first "$tier"
    up $second "$tier"
    record $second "$tier"
    measure $second "$tier"
    down $second "$tier"
done

if [ "$RESTORE" = 1 ]; then
    log "PROGRESS phase=restore (parity pins)"
    docker compose -f "$REPO/docker-compose.yml" -f "$SUITE/docker-compose.egernia-pins.yml" \
        up -d --no-build db tap-api tap-executor
    docker compose -f "$SUITE/docker-compose.dachs.yml" up -d --no-build
fi
log "PROGRESS complete run=$RUN_NAME"
log "publish with: uv run --group tap-compare python benchmarks/tap-compare publish --run $RUN_NAME"
