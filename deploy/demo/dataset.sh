#!/usr/bin/env bash
# The demo's ~100 GiB dataset: generate it once, snapshot it, restore it in
# minutes every time after.
#
# Generation runs the benchmark suite's generator against the in-cluster
# database through a port-forward. Every row therefore crosses that
# port-forward, so run this from a machine with a fast path to the cluster —
# not from the laptop that will present the demo over hotel wifi. It is
# resumable and checkpointed: a run that dies at 60 GiB restarts near 60, not
# at zero, so an interrupted generation is an inconvenience rather than a
# lost evening.
set -euo pipefail

action=${1:-generate}
namespace=${2:-egernia-demo}
release=${3:-egernia}
tier=${TIER:-D5}
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
bench_dir="$repo_root/benchmarks/egernia-performance"
python_bin=${PYTHON:-$repo_root/.venv/bin/python}
local_port=${LOCAL_PORT:-15432}

pg_pod() {
  kubectl get pod -n "$namespace" -l app.kubernetes.io/component=postgresql \
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
  echo "generating tier $tier into '$namespace' (context $(kubectl config current-context))"
  echo "this is hours the first time; it is resumable, and 'snapshot' makes it once"

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

  # dataset.build() takes a DSN; only ensure_dataset() is bound to the kind
  # cluster's port-forward, and that is the one thing a remote cluster cannot
  # reuse. Markers are written beside the repo so a resumed run finds them.
  markers=${MARKER_DIR:-$repo_root/.demo-datasets}
  cd "$bench_dir"
  PYTHONPATH="$bench_dir:$repo_root" DSN="$dsn" TIER="$tier" MARKERS="$markers" \
    "$python_bin" - <<'PY' || { echo; echo "interrupted — re-run to resume where it stopped" >&2; exit 1; }
import logging, os, pathlib, sys
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
from egernia_bench.dataset import generate as dataset_mod
from egernia_bench.orchestrate import runner

cfg = runner.load_config()["datasets"]
tier = os.environ["TIER"]
targets = [d for d in cfg["datasets"] if d["name"] == tier]
if not targets:
    sys.exit(f"no dataset tier named {tier} in config/datasets.yaml")
# every tier below the target too: the database grows through them in order,
# so asking for D5 alone would still have to pass through D1-D4's rows
wanted = [d for d in cfg["datasets"] if d["target_bytes"] <= targets[0]["target_bytes"]]
stats = dataset_mod.build(os.environ["DSN"], cfg, wanted, pathlib.Path(os.environ["MARKERS"]))
for stat in stats:
    print(f"  {stat.name}: {stat.database_bytes / 2**30:.1f} GiB")
PY

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
