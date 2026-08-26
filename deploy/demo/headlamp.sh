#!/usr/bin/env bash
# Headlamp, so the cluster view is on the screen beside the notebook.
#
# Installed with a read-only ClusterRole binding by default: a demo audience
# watching pods appear needs to see the cluster, not to be able to change it,
# and a dashboard with cluster-admin on a projector is a bad habit to teach.
set -euo pipefail

action=${1:-install}
namespace=${2:-egernia-demo}
release=${3:-headlamp}
readonly_mode=${HEADLAMP_READONLY:-1}

case "$action" in
install)
  echo "installing Headlamp into namespace '$namespace' on context '$(kubectl config current-context)'"
  helm repo add headlamp https://kubernetes-sigs.github.io/headlamp/ >/dev/null 2>&1 || true
  helm repo update headlamp >/dev/null

  helm upgrade --install "$release" headlamp/headlamp \
    --namespace "$namespace" --create-namespace \
    --set fullnameOverride=headlamp \
    --wait --timeout 5m

  if [ "$readonly_mode" = "1" ]; then
    # `view` is a built-in ClusterRole: every get/list/watch, no mutation.
    kubectl create clusterrolebinding headlamp-view \
      --clusterrole=view \
      --serviceaccount="$namespace:headlamp" \
      --dry-run=client -o yaml | kubectl apply -f -
    echo "bound the built-in 'view' ClusterRole (read-only)."
    echo "  set HEADLAMP_READONLY=0 to skip this and bind your own role instead"
  else
    echo "no role bound: Headlamp will show only what its ServiceAccount can already see"
  fi

  echo
  echo "a token to sign in with (expires in 24h):"
  kubectl create token headlamp --namespace "$namespace" --duration=24h 2>/dev/null \
    || echo "  (could not mint one: kubectl create token needs Kubernetes 1.24+)"
  echo
  echo "reach it with:"
  echo "  kubectl port-forward -n $namespace service/headlamp 8081:80"
  echo "  open http://localhost:8081"
  echo
  echo "Port-forward rather than an Ingress on purpose: Headlamp authenticates"
  echo "by bearer token, and publishing it needs a decision this script will"
  echo "not take for you."
  ;;
uninstall)
  helm uninstall "$release" --namespace "$namespace" || true
  kubectl delete clusterrolebinding headlamp-view --ignore-not-found
  ;;
*)
  echo "usage: $0 [install|uninstall] [namespace] [release]" >&2
  exit 2
  ;;
esac
