#!/usr/bin/env bash
# The two lines that put the service on the notebook machine's localhost.
#
# The ingress routes by Host as well as by path, so the name has to be in the
# request rather than only in the URL bar — which is what the /etc/hosts line
# is for. `.test` is reserved by RFC 6761 for exactly this and can never
# collide with a real domain.
set -uo pipefail

namespace=${1:-egernia-demo}
host=${2:-}                       # the SSH host the cluster is reachable at
local_port=${LOCAL_PORT:-8080}
ingress_host=${INGRESS_HOST:-egernia.test}

# Where the ingress controller can be reached *from the cluster host*. Three
# shapes, in the order they are worth trying.
target=""
mode=""

# 1. a NodePort on the controller: the cluster host reaches it on localhost
np=$(kubectl get svc -A -o jsonpath='{range .items[?(@.spec.type=="NodePort")]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{range .spec.ports[?(@.port==80)]}{.nodePort}{end}{"\n"}{end}' 2>/dev/null \
     | grep -iE 'ingress|nginx|traefik' | awk -F'\t' '$3!=""{print $3; exit}')
if [ -n "$np" ]; then
  target="localhost:$np"
  mode="the ingress controller's NodePort $np"
fi

# 2. a LoadBalancer with an address the cluster host can route to
if [ -z "$target" ]; then
  lb=$(kubectl get svc -A -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.name}{"\t"}{.status.loadBalancer.ingress[0].ip}{"\n"}{end}' 2>/dev/null \
       | grep -iE 'ingress|nginx|traefik' | awk -F'\t' '$2!=""{print $2; exit}')
  if [ -n "$lb" ]; then
    target="$lb:80"
    mode="the ingress controller's LoadBalancer address $lb"
  fi
fi

# 3. nothing exposed: the cluster host has to port-forward first
if [ -z "$target" ]; then
  svc=$(kubectl get svc -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{"\n"}{end}' 2>/dev/null \
        | grep -iE 'ingress-nginx-controller|traefik' | head -1)
  cat <<MSG
No ingress controller is exposed on a node port or a load balancer, so there
is nothing for the tunnel to land on directly.

${svc:+On the cluster host, forward it first:
  kubectl port-forward -n ${svc%%/*} svc/${svc##*/} 8080:80

then from this machine:
  ssh -N -L $local_port:localhost:8080 ${host:-you@cluster-host}
}
${svc:+}${svc:-Install one (ingress-nginx is the usual choice), or fall back to NodePort:
  helm upgrade ... --set ingress.enabled=false \\
      --set tapApi.service.type=NodePort --set tapApi.service.nodePort=30080
  ssh -N -L $local_port:localhost:30080 you@cluster-host
and set EGERNIA_BASE_URL=http://localhost:$local_port (no Host routing, so
/prometheus will not be reachable — expose it on its own NodePort too).}
MSG
  exit 1
fi

echo "the ingress is reachable from the cluster host at $target ($mode)"
echo
echo "1. on THIS machine, once:"
echo "     echo '127.0.0.1 $ingress_host' | sudo tee -a /etc/hosts"
echo
echo "2. open the tunnel (leave it running):"
echo "     ssh -N -L $local_port:$target ${host:-you@cluster-host}"
echo
echo "3. everything is then at:"
echo "     http://$ingress_host:$local_port/tap          IVOA TAP"
echo "     http://$ingress_host:$local_port/api/v1       JSON API"
echo "     http://$ingress_host:$local_port/prometheus   metrics"
echo
echo "4. point the notebook at it:"
echo "     make demo-notebook BASE_URL=http://$ingress_host:$local_port"
echo
echo "Use the name, not 127.0.0.1: the ingress matches on the Host header, and"
echo "a request to http://localhost:$local_port carries the wrong one."

if [ -n "$host" ]; then
  echo
  read -r -p "open the tunnel now? [y/N] " reply
  case "$reply" in
    [yY]*) exec ssh -N -L "$local_port:$target" "$host" ;;
    *) echo "not opened." ;;
  esac
fi
