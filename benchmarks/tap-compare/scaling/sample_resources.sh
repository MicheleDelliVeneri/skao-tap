#!/usr/bin/env bash
# Resource telemetry beside a scaling run (PROTOCOL.md, amendment 1): every
# INTERVAL seconds, one JSON line per running egernia-* / tap-compare-dachs-*
# container with its cgroup v2 CPU time and memory, plus the number of
# python processes in it (the API's uvicorn supervisor + workers). Pure
# file reads; `docker inspect` only once per new container id.
#
#   nohup setsid taskset -c 24-29 sample_resources.sh <run-dir>/resources.jsonl &
set -u
OUT=$1
INTERVAL=${INTERVAL:-5}
declare -A NAME
while true; do
    now=$(date +%s.%N)
    for scope in /sys/fs/cgroup/system.slice/docker-*.scope; do
        [ -d "$scope" ] || continue
        id=${scope##*/docker-}; id=${id%.scope}
        if [ -z "${NAME[$id]+x}" ]; then
            NAME[$id]=$(docker inspect --format '{{.Name}}' "$id" 2>/dev/null | sed 's|^/||')
        fi
        name=${NAME[$id]}
        case $name in egernia-prometheus-*) continue ;; egernia-*|tap-compare-dachs-*) ;; *) continue ;; esac
        cpu=$(awk '$1 == "usage_usec" {print $2}' "$scope/cpu.stat" 2>/dev/null) || continue
        mem=$(cat "$scope/memory.current" 2>/dev/null) || continue
        procs=0
        for pid in $(cat "$scope/cgroup.procs" 2>/dev/null); do
            if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q python; then procs=$((procs + 1)); fi
        done
        printf '{"t": %s, "container": "%s", "cpu_usec": %s, "mem_bytes": %s, "python_procs": %s}\n' \
            "$now" "$name" "${cpu:-0}" "${mem:-0}" "$procs"
    done >> "$OUT"
    sleep "$INTERVAL"
done
