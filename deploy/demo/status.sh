#!/usr/bin/env bash
# Where the demo is reachable and what state it is in — the one command to
# run before standing up in front of anyone.
set -uo pipefail

namespace=${1:-egernia-demo}
release=${2:-egernia}
host=${3:-}

echo "context:   $(kubectl config current-context 2>/dev/null)"
echo "namespace: $namespace"
echo

echo "pods"
kubectl get pods -n "$namespace" -o wide --no-headers 2>/dev/null \
  | awk '{printf "  %-42s %-10s %-9s %s\n", $1, $3, $5, $7}' || echo "  (none)"

echo
echo "autoscaling"
if kubectl get hpa -n "$namespace" --no-headers >/dev/null 2>&1; then
  kubectl get hpa -n "$namespace" --no-headers 2>/dev/null \
    | awk '{printf "  %-34s %-14s min=%s max=%s current=%s\n", $1, $3, $4, $5, $6}'
  # <unknown> here is the whole demo failing quietly: the HPA cannot read CPU,
  # so it will never scale and the dashboard will show a flat line.
  if kubectl get hpa -n "$namespace" -o jsonpath='{.items[*].status.currentMetrics}' 2>/dev/null | grep -q 'null'; then
    echo "  ! a metric reads <unknown> — is metrics-server installed?"
  fi
else
  echo "  (no HorizontalPodAutoscaler)"
fi

echo
echo "storage"
kubectl get pvc -n "$namespace" --no-headers 2>/dev/null \
  | awk '{printf "  %-40s %-9s %s\n", $1, $2, $4}' || echo "  (none)"

echo
echo "database"
pod=$(kubectl get pod -n "$namespace" -l app.kubernetes.io/component=postgres \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$pod" ]; then
  size=$(kubectl exec -n "$namespace" "$pod" -- psql -U tap -d tap -tAc \
         "SELECT pg_size_pretty(pg_database_size('tap'))" 2>/dev/null)
  rows=$(kubectl exec -n "$namespace" "$pod" -- psql -U tap -d tap -tAc \
         "SELECT count(*) FROM ivoa.obscore" 2>/dev/null)
  echo "  size:            ${size:-unknown}"
  echo "  ivoa.obscore:    ${rows:-unknown} rows"
else
  echo "  (no PostgreSQL pod)"
fi

echo
echo "endpoints"
address=$(kubectl get ingress -n "$namespace" -o jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}{.items[0].status.loadBalancer.ingress[0].hostname}' 2>/dev/null)
ingress_host=${host:-$(kubectl get ingress -n "$namespace" -o jsonpath='{.items[0].spec.rules[0].host}' 2>/dev/null)}
if [ -n "$ingress_host" ]; then
  echo "  TAP:         http://$ingress_host/tap"
  echo "  JSON API:    http://$ingress_host/api/v1"
  if [ -n "$address" ]; then
    echo "  ingress address: $address"
    echo "  the notebook machine must resolve '$ingress_host' to it (DNS, or /etc/hosts)"
  else
    echo "  the Ingress has no external address — expected when the controller"
    echo "  is ClusterIP (reach it with a port-forward), a problem if you were"
    echo "  expecting a LoadBalancer. tunnel.sh says which applies here."
  fi
else
  echo "  (no Ingress)"
fi

echo
if [ -n "$ingress_host" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$ingress_host/tap/availability" 2>/dev/null)
  if [ "$code" = "200" ]; then
    echo "reachable: /tap/availability answered 200 from this machine"
  else
    echo "not reachable from this machine (/tap/availability gave '${code:-no answer}')."
    echo "That may be fine — this machine is not necessarily the notebook's."
  fi
fi
