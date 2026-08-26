"""A static OpenID Connect issuer, in the cluster, for the authenticated rung.

Package 18's second half: no benchmark in this suite has ever enabled
authentication, so every capacity figure published from it is an
unauthenticated figure — while a deployment that gates its endpoints verifies
a bearer token on exactly the CPU-bound path the numbers describe.

Measuring that needs an issuer the service will actually trust, because the
cost being measured is the real one: `IAMTokenVerifier.verify` fetches the
issuer's discovery document, caches its JWKS, and then checks an RS256
signature, the issuer, the expiry and the audience on *every* request — there
is no verified-token cache in the service, deliberately. Pointing the service
at a fake that skips any of that would measure a fake.

So this deploys the smallest thing that is not a fake: a keypair generated for
the run, its public half published as a JWKS by `http.server` behind a
Service, and a discovery document naming itself as the issuer. Tokens are
minted on the host with the private half. Everything the service does with
them — discovery, key lookup, signature, claims — is what it would do against
an INDIGO IAM; what is missing is only the IAM's own latency, which is paid
once per JWKS lifetime rather than per request.

The private key never reaches the repository or the cluster: it is generated
in this process, used to mint tokens here, and discarded when the run ends. A
checked-in test key would be a credential in git that happens to be
worthless, which is not a distinction worth relying on.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import subprocess
import time

from .. import cluster

log = logging.getLogger("egernia_bench.oidc")

NAME = "egernia-oidc"
NAMESPACE = "default"
PORT = 8080

#: The issuer as the *service* must see it, which is also what the discovery
#: document and every token's `iss` claim must say. In-cluster DNS rather than
#: the host's NodePort: the token is verified inside the pod, and an issuer
#: string that only resolves from the host would make the verifier's discovery
#: fetch the thing under measurement.
ISSUER = f"http://{NAME}.{NAMESPACE}.svc.cluster.local:{PORT}"

#: One IAM issues tokens to many services, so the service refuses to verify
#: without an expected audience (or an explicit opt-out). Named for the suite.
AUDIENCE = "egernia-bench"

#: The group the minted tokens carry, and the one config/auth-values.yaml
#: grants every gated operation to. Rooted, like IAM group names.
GROUP = "/egernia/bench"

#: A scope as well, so the authorisation rung exercises the group path rather
#: than accidentally passing on a scope match.
SCOPE = "egernia:query"


def _b64(value: int) -> str:
    """An RSA parameter, base64url without padding, as JWK requires."""
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclasses.dataclass(frozen=True)
class Issuer:
    """A keypair, the documents that publish it, and how to sign for it."""

    kid: str
    private_pem: bytes
    jwks: dict
    discovery: dict
    issuer: str = ISSUER
    audience: str = AUDIENCE

    def mint(
        self,
        *,
        subject: str = "bench-principal",
        lifetime_s: int = 6 * 3600,
        groups: tuple[str, ...] = (GROUP,),
        scopes: tuple[str, ...] = (SCOPE,),
    ) -> str:
        """One access token, signed for this issuer.

        Long-lived by construction: a token that expires mid-window would turn
        a capacity measurement into a measurement of 401s, and the expiry check
        costs the same whether the deadline is a minute away or a day. One
        token is reused for every request in a rung, which is what a client
        does — and the service still verifies the signature on each one,
        because it caches keys and never principals.
        """
        import jwt

        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "sub": subject,
            "aud": self.audience,
            "iat": now,
            "nbf": now,
            "exp": now + lifetime_s,
            # WLCG/INDIGO IAM shape: the claims the service is configured to
            # read (TAP_IAM_GROUP_CLAIMS defaults to groups,wlcg.groups).
            "groups": list(groups),
            "wlcg.groups": list(groups),
            "scope": " ".join(scopes),
            "client_id": "egernia-bench",
            "token_type": "Bearer",
        }
        return jwt.encode(claims, self.private_pem, algorithm="RS256", headers={"kid": self.kid})


def keypair(bits: int = 2048, issuer_url: str = ISSUER) -> Issuer:
    """Generate the run's issuer.

    2048-bit RSA because that is what an IAM issues and what the verification
    cost is therefore a cost of. A smaller key would make the number this
    exists to produce optimistic, and a larger one would make it a number
    about a configuration nobody runs.

    ``issuer_url`` is the in-cluster Service for a run. It is a parameter only
    so that the same documents can be served from a loopback port and verified
    by the service's real ``IAMTokenVerifier`` in a test: the one thing worth
    proving about this module is that a token it mints verifies against a JWKS
    it publishes, and an authenticated rung whose tokens were refused would
    otherwise report the cost of returning 401s.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    numbers = key.public_key().public_numbers()
    kid = "bench-" + _b64(numbers.n)[:16]
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _b64(numbers.n),
                "e": _b64(numbers.e),
            }
        ]
    }
    discovery = {
        # Checked against TAP_IAM_ISSUER by the verifier: a discovery document
        # naming another issuer is how a misconfigured URL would move trust to
        # a different IAM, so it refuses rather than follows.
        "issuer": issuer_url,
        "jwks_uri": f"{issuer_url}/jwks.json",
        "authorization_endpoint": f"{issuer_url}/authorize",
        "token_endpoint": f"{issuer_url}/token",
        "response_types_supported": ["code", "token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", SCOPE],
        "grant_types_supported": ["client_credentials", "authorization_code"],
    }
    return Issuer(
        kid=kid,
        private_pem=private_pem,
        jwks=jwks,
        discovery=discovery,
        issuer=issuer_url,
    )


def manifest(issuer: Issuer, image: str) -> str:
    """The stub as Kubernetes objects.

    Runs the *API's own image* rather than a public one. It is already loaded
    into the node (this run built it), it contains a Python, and pulling
    anything else would make the authenticated rung depend on registry access
    that no other family needs.
    """
    # Rendered as JSON rather than as YAML text: the documents are JSON with
    # braces and quotes in them, and hand-indenting those into a YAML block
    # scalar is the kind of thing that works until a key contains a colon.
    # kubectl reads JSON wherever it reads YAML, JSON being a subset of it.
    objects = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{NAME}-documents", "namespace": NAMESPACE},
            "data": {
                "openid-configuration": json.dumps(issuer.discovery, indent=2),
                "jwks.json": json.dumps(issuer.jwks, indent=2),
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": NAME, "namespace": NAMESPACE},
            "spec": {
                "selector": {"app": NAME},
                "ports": [{"name": "http", "port": PORT, "targetPort": PORT}],
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": NAME, "namespace": NAMESPACE},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": NAME}},
                "template": {
                    "metadata": {"labels": {"app": NAME}},
                    "spec": {
                        "containers": [
                            {
                                "name": "issuer",
                                "image": image,
                                "imagePullPolicy": "Never",
                                "command": [
                                    "/srv/.venv/bin/python",
                                    "-m",
                                    "http.server",
                                    str(PORT),
                                    "--directory",
                                    "/srv/oidc",
                                ],
                                "ports": [{"containerPort": PORT}],
                                # Small on purpose: this must not be a
                                # meaningful consumer of the node's CPU while
                                # the thing beside it is being measured.
                                "resources": {
                                    "requests": {"cpu": "50m", "memory": "64Mi"},
                                    "limits": {"cpu": "500m", "memory": "256Mi"},
                                },
                                "volumeMounts": [{"name": "documents", "mountPath": "/srv/oidc"}],
                                "readinessProbe": {
                                    "httpGet": {
                                        "path": "/.well-known/openid-configuration",
                                        "port": PORT,
                                    },
                                    "initialDelaySeconds": 1,
                                    "periodSeconds": 2,
                                },
                            }
                        ],
                        "volumes": [
                            {
                                "name": "documents",
                                "configMap": {
                                    "name": f"{NAME}-documents",
                                    "items": [
                                        {
                                            "key": "openid-configuration",
                                            # where OIDC discovery looks, and
                                            # a path a ConfigMap item may name
                                            "path": ".well-known/openid-configuration",
                                        },
                                        {"key": "jwks.json", "path": "jwks.json"},
                                    ],
                                },
                            }
                        ],
                    },
                },
            },
        },
    ]
    return "\n---\n".join(json.dumps(obj, indent=2) for obj in objects)


def deploy(issuer: Issuer, image: str, *, timeout_s: int = 180) -> None:
    """Apply the stub and wait for it to serve its documents."""
    payload = manifest(issuer, image)
    subprocess.run(
        [
            "kubectl",
            "--context",
            f"kind-{cluster.CLUSTER}",
            "apply",
            "-f",
            "-",
        ],
        input=payload,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    cluster.kubectl(
        "rollout", "status", f"deploy/{NAME}", "-n", NAMESPACE, f"--timeout={timeout_s}s"
    )
    log.info("OIDC stub issuer ready at %s", ISSUER)


def remove() -> None:
    """Delete the stub. Idempotent; a leftover would outlive the run's key."""
    for kind, name in (
        ("deployment", NAME),
        ("service", NAME),
        ("configmap", f"{NAME}-documents"),
    ):
        cluster.kubectl("delete", kind, name, "-n", NAMESPACE, "--ignore-not-found", check=False)
