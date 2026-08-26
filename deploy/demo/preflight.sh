#!/usr/bin/env bash
# What the cluster is, and what it lacks, before anything is installed.
#
# Every check here is one that fails slowly and confusingly if left to
# discovery: a missing StorageClass strands a 200Gi PVC in Pending, a missing
# ingress controller leaves an Ingress with no address and no error, and an
# HPA without metrics-server reports <unknown>/70% forever while the demo's
# whole claim is that it scales.
set -uo pipefail

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED=1; }
FAILED=0

context=$(kubectl config current-context 2>/dev/null)
if [ -z "$context" ]; then
  echo "no current kubectl context — set one with 'kubectl config use-context'" >&2
  exit 2
fi

echo "cluster"
echo "  context: $context"
# /version is the server's own, unlike `kubectl version`, which also prints
# the client's — and the client's is there even when the cluster is not.
# Newlines stripped first: the endpoint pretty-prints.
server=$(kubectl get --raw /version 2>/dev/null | tr -d ' \n' \
         | sed -n 's/.*"gitVersion":"\([^"]*\)".*/\1/p')
[ -n "$server" ] && ok "reachable, server $server" || bad "cannot reach the API server"

nodes=$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [ "${nodes:-0}" -gt 1 ]; then
  ok "$nodes nodes — pods can land on different machines"
elif [ "${nodes:-0}" -eq 1 ]; then
  warn "1 node: the demo runs, but 'it scales across machines' will not be literally true"
else
  bad "no nodes visible"
fi

echo
echo "capacity (allocatable, whole cluster)"
kubectl get nodes -o jsonpath='{range .items[*]}  {.metadata.name}{"\t"}{.status.allocatable.cpu}{" cpu\t"}{.status.allocatable.memory}{"\n"}{end}' 2>/dev/null

echo
echo "storage"
default_sc=$(kubectl get storageclass -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{"\n"}{end}' 2>/dev/null | awk -F'\t' '$2=="true"{print $1}')
classes=$(kubectl get storageclass --no-headers 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
if [ -n "$default_sc" ]; then
  ok "default StorageClass: $default_sc"
elif [ -n "$classes" ]; then
  warn "no default StorageClass; pass STORAGE_CLASS=<one of: $classes>"
else
  bad "no StorageClass at all — a 200Gi PVC will sit in Pending forever"
fi

echo
echo "ingress"
classes=$(kubectl get ingressclass --no-headers 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
if [ -n "$classes" ]; then
  ok "IngressClass available: $classes"
  echo "     pass INGRESS_CLASS=<name> unless one is marked default"
else
  bad "no IngressClass — the notebook machine will have nothing to connect to"
fi

echo
echo "autoscaling"
if kubectl get deployment metrics-server -n kube-system >/dev/null 2>&1 \
   || kubectl get apiservice v1beta1.metrics.k8s.io >/dev/null 2>&1; then
  ok "metrics-server present — the HPA can read CPU"
else
  bad "no metrics-server: the HPA will report <unknown>/70% and never scale"
fi

echo
echo "permissions"
for verb in create; do
  for res in namespace deployment ingress persistentvolumeclaim clusterrole; do
    if [ "$(kubectl auth can-i $verb $res 2>/dev/null)" = "yes" ]; then
      ok "can $verb $res"
    else
      bad "cannot $verb $res — the chart creates one"
    fi
  done
done

echo
if [ "$FAILED" -eq 0 ]; then
  echo "ready. next: make demo-deploy HOST=<hostname you can resolve>"
else
  echo "not ready — the ✗ lines above will each fail slowly and unhelpfully if ignored" >&2
  exit 1
fi
