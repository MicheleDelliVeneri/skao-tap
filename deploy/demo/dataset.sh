#!/usr/bin/env bash
# The demo's dataset: generate it once, snapshot it, restore it in minutes
# every time after.
#
# This fills the models the service implements — the ODP hierarchy and the
# software discovery model — and leaves ivoa.obscore as the plugin's view over
# them. Deliberately *not* the benchmark suite's generator: that one builds a
# CAOM hierarchy and replaces ivoa.obscore with a table of its own, which is
# right for measuring throughput against a fixed corpus and wrong for showing
# what the service does. See odp_dataset.py.
#
# Every row crosses a port-forward, so run this from a machine with a fast
# path to the cluster — not from the laptop that will present the demo over
# hotel wifi.
set -euo pipefail

action=${1:-generate}
namespace=${2:-egernia-demo}
release=${3:-egernia}
tier=${TIER:-D5}
projects=${PROJECTS:-4700}
software=${SOFTWARE:-400}
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
python_bin=${PYTHON:-$repo_root/.venv/bin/python}
local_port=${LOCAL_PORT:-15432}

pg_pod() {
  kubectl get pod -n "$namespace" -l app.kubernetes.io/component=postgres \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

pg_pvc() {
  kubectl get pvc -n "$namespace" -o name 2>/dev/null | grep -i postgres | head -1
}

require_pod() {
  local pod
  pod=$(pg_pod)
  if [ -z "$pod" ]; then
    echo "no PostgreSQL pod in namespace '$namespace' — deploy first" >&2
    exit 1
  fi
  echo "$pod"
}

case "$action" in
generate)
  pod=$(require_pod)
  echo "generating $projects ODP projects and $software software packages into"
  echo "'$namespace' (context $(kubectl config current-context))"

  kubectl port-forward -n "$namespace" "pod/$pod" "$local_port:5432" >/dev/null 2>&1 &
  forward=$!
  # shellcheck disable=SC2064
  trap "kill $forward 2>/dev/null || true" EXIT
  for _ in $(seq 1 30); do
    if (echo >"/dev/tcp/127.0.0.1/$local_port") >/dev/null 2>&1; then break; fi
    sleep 1
  done

  password=$(kubectl get secret -n "$namespace" "$release" \
             -o jsonpath='{.data.postgres-password}' 2>/dev/null | base64 -d 2>/dev/null || echo tap)
  dsn="postgresql://tap:${password}@127.0.0.1:${local_port}/tap"

  # --truncate so a re-run replaces the corpus rather than colliding with it
  # on the primary keys: the generator is deterministic, so the same seed
  # would otherwise reinsert rows that are already there.
  "$python_bin" "$repo_root/deploy/demo/odp_dataset.py" \
    --dsn "$dsn" --projects "$projects" --software "$software" --truncate

  echo
  echo "done. capture it so this never has to run again:"
  echo "  make demo-snapshot"
  ;;

snapshot)
  pvc=$(pg_pvc)
  [ -n "$pvc" ] || { echo "no PostgreSQL PVC found in '$namespace'" >&2; exit 1; }
  name="egernia-demo-$tier-$(date -u +%Y%m%dT%H%M%SZ)"

  if ! kubectl get crd volumesnapshots.snapshot.storage.k8s.io >/dev/null 2>&1; then
    cat >&2 <<'MSG'
This cluster has no VolumeSnapshot CRDs, so the fast path is unavailable.

A 100 GiB pg_dump is possible but slow to take and slower to restore, which
defeats the purpose. Either install the external-snapshotter CRDs and a CSI
driver that supports snapshots, or keep the namespace between demos:

  make demo-teardown KEEP_DATA=1
MSG
    exit 1
  fi

  echo "snapshotting ${pvc#persistentvolumeclaim/} as $name"
  kubectl apply -f - <<YAML
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: $name
  namespace: $namespace
  labels:
    egernia.demo/tier: "$tier"
spec:
  source:
    persistentVolumeClaimName: ${pvc#persistentvolumeclaim/}
YAML
  echo
  echo "restore it into a fresh deployment with:"
  echo "  make demo-restore SNAPSHOT=$name"
  ;;

restore)
  snapshot=${SNAPSHOT:-$(kubectl get volumesnapshot -n "$namespace" \
    -l "egernia.demo/tier=$tier" -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null)}
  [ -n "$snapshot" ] || { echo "no snapshot found; pass SNAPSHOT=<name>" >&2; exit 1; }

  cat <<MSG
Restoring means the database PVC has to be created *from* the snapshot before
the chart claims it, because a PVC's dataSource cannot be added afterwards.

  1. make demo-teardown            (the release; the snapshot survives it)
  2. kubectl apply -f - <<'YAML'
     apiVersion: v1
     kind: PersistentVolumeClaim
     metadata:
       name: data-$release-postgresql-0
       namespace: $namespace
     spec:
       accessModes: [ReadWriteOnce]
       resources:
         requests:
           storage: 200Gi
       dataSource:
         name: $snapshot
         kind: VolumeSnapshot
         apiGroup: snapshot.storage.k8s.io
     YAML
  3. make demo-deploy HOST=<host>  (the StatefulSet adopts the existing PVC)

Printed rather than run: step 2 hard-codes a PVC name the chart's StatefulSet
must agree with, and getting that wrong silently gives you an empty database
that looks like a successful demo right up until the first query.
MSG
  ;;

*)
  echo "usage: $0 [generate|snapshot|restore] [namespace] [release]" >&2
  exit 2
  ;;
esac
