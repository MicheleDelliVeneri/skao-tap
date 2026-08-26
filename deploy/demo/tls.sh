#!/usr/bin/env bash
# A self-signed certificate for the demo host, for anyone who wants the
# padlock in the screenshot.
#
# Worth being clear that it buys appearance rather than security here: the
# only way to this service is an SSH tunnel, which is already an encrypted,
# authenticated channel. TLS inside it protects nothing the tunnel does not
# already protect, and costs a trust decision on every laptop that connects.
# It is here because "why is it not https" is a fair question to be asked in
# front of an audience, and answering it with a working padlock is quicker
# than answering it with this paragraph.
set -euo pipefail

namespace=${1:-egernia-demo}
host=${2:-egernia.test}
secret=${SECRET_NAME:-egernia-demo-tls}
days=${DAYS:-365}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$work/tls.key" -out "$work/tls.crt" \
  -days "$days" -subj "/CN=$host" \
  -addext "subjectAltName=DNS:$host,DNS:localhost,IP:127.0.0.1" \
  2>/dev/null

kubectl create secret tls "$secret" \
  --namespace "$namespace" \
  --cert="$work/tls.crt" --key="$work/tls.key" \
  --dry-run=client -o yaml | kubectl apply -f -

cat <<MSG

Created secret '$secret' in '$namespace' for $host (valid $days days).

Redeploy with TLS on:

  make demo-deploy HOST=$host TLS_SECRET=$secret

The certificate is self-signed, so a browser will warn once and the notebook
would refuse outright. Either trust it on this machine:

  kubectl get secret $secret -n $namespace \\
    -o jsonpath='{.data.tls\\.crt}' | base64 -d > egernia-demo.crt
  # then add egernia-demo.crt to the OS or browser trust store

or tell the notebook to skip verification, which is defensible only because
the tunnel underneath is already doing the job:

  make demo-notebook BASE_URL=https://$host:8443 INSECURE_TLS=1
MSG
