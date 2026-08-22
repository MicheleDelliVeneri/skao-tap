# Authentication and authorisation

Off by default. A deployment that sets nothing behaves exactly as it did
before this existed: every endpoint is open, and the service logs a warning
at startup saying so.

When enabled, two separable things happen to a request:

1. **Authentication** — if the request carries a bearer token, this service
   verifies it against the IAM's published signing keys. Always, whatever
   else is configured. A forged, expired, wrong-audience or wrong-issuer
   token is rejected with `401` even on an endpoint that needed no token:
   an unverifiable credential is an error, never silently "anonymous".
2. **Authorisation** — for the gated operations, a plugin decides whether the
   verified principal may proceed. That decision is where deployments differ,
   so it sits behind a plugin interface.

## What is gated

| Operation | Endpoint |
| --- | --- |
| `metadata.ingest` | `POST /api/v1/<mount>` |
| `metadata.amend` | `PATCH /api/v1/<mount>/{root_id}` |
| `metadata.delete` | `DELETE /api/v1/<mount>/{root_id}` |

Everything else — every `GET`, and querying through `POST /tap/sync`,
`POST /tap/async`, `POST /api/v1/query` and `POST /api/v1/jobs` — stays open
to anonymous callers. This is deliberate: TAP clients send ADQL as a POST
body, so gating "all writes" by HTTP method would lock PyVO, TOPCAT and
every other standard VO client out of an authenticated deployment. What is
protected is the data a caller can *change*.

`GET /api/v1/auth` reports what the deployment enforces, so clients need not
discover it by trial:

```json
{"enabled": true, "plugin": "permissions-api",
 "issuer": "https://ska-iam.stfc.ac.uk", "audience": "science-metadata",
 "gated_operations": {"metadata.ingest": "POST /api/v1/<mount>", ...}}
```

## Token verification

Tokens are JWT access tokens from an OpenID Connect provider — INDIGO IAM in
SRCNet. The service fetches `<issuer>/.well-known/openid-configuration`,
takes `jwks_uri` from it, and verifies each token's signature against those
keys, along with `iss`, `exp` and — when an audience is configured — `aud`.
Discovery and keys are cached (`auth.iam.jwksCacheSeconds`); an unknown key
id triggers at most one refetch per cache window, so a flood of bogus tokens
cannot be turned into a flood of requests to the IAM.

If the discovery document names a different issuer than the one configured,
the service refuses to use it, rather than letting a mistyped URL move trust
to another IAM.

Group membership is read from whichever claim the IAM populates (`groups`,
`wlcg.groups`, `entitlements`, `eduperson_entitlement`) and normalised to a
leading slash, so `/ska/oper` and `ska/oper` in configuration mean the same
thing.

## Choosing a plugin

Authorisation is a plugin because SRCNet's answer is not everyone's. Plugins
are discovered through the `skao_tap.auth` entry-point group — the same
mechanism as the [metadata domains](plugins.md) — and `auth.plugin` selects
one.

### `iam-groups` (default)

Decides locally from the verified token. Each operation names the groups and
scopes that grant it; any one of them is enough:

```yaml
auth:
  enabled: true
  plugin: iam-groups
  iam:
    issuer: https://ska-iam.stfc.ac.uk
    audience: science-metadata
  roles:
    metadata.ingest:
      groups: ["/ska/science-metadata/oper"]
    metadata.amend:
      groups: ["/ska/science-metadata/oper"]
    metadata.delete:
      groups: ["/ska/science-metadata/admin"]
      scopes: ["science-metadata:admin"]
```

An operation with empty `groups` and `scopes` accepts any verified token. An
operation left out of the mapping entirely is **denied** — an operation
nobody configured should not be open by omission, least of all deletion.

### `permissions-api`

Delegates to the [SKA SRC Permissions
API](https://gitlab.com/ska-telescope/src/src-service-apis/ska-src-permissions-api),
which is how SRCNet services authorise. No policy lives in this repository:
IAM groups are bound to roles, and roles to routes, in the Permissions API's
own policy files.

```yaml
auth:
  enabled: true
  plugin: permissions-api
  iam:
    issuer: https://ska-iam.stfc.ac.uk
  permissionsApi:
    url: https://permissions.srcnet.skao.int/api/v1
    serviceName: science-metadata
    serviceVersion: "1"
```

Per gated request the service calls

```
POST {url}/authorise/route/{serviceName}?route=<templated path>&method=<METHOD>&version=<v>&token=<token>
```

with the request's path parameters as the JSON body, and honours
`{"is_authorised": bool}`. That is the contract the Data Management API uses
through the vendored `ska_src_permissions_api` client; it is spoken directly
here because those packages are published to SKA's internal index rather
than PyPI, and one POST does not justify a private index in every build.

Two deliberate differences from DMAPI's usage:

- **the token is verified before it is sent.** DMAPI forwards the bearer
  token and lets the Permissions API judge it. Here the IAM signature check
  happens first, so the Permissions API is asked what a known principal may
  do — never asked to vouch for an unknown string.
- **an unreachable Permissions API is an outage, not a denial.** It answers
  `500`, not `403`: a policy service that is down has not decided anything,
  and reporting it as "you are not allowed" would send operators hunting
  through group memberships instead of at the outage.

### Writing your own

```python
from tapcore.auth import AuthPlugin

class MyPolicy(AuthPlugin):
    name = "my-policy"

    def authorize(self, principal, operation, context) -> bool:
        # principal.subject / .groups / .scopes / .claims are already verified
        return "admin" in principal.groups
```

```toml
[project.entry-points."skao_tap.auth"]
my-policy = "my_package.policy:MyPolicy"
```

Install it alongside the service and set `auth.plugin: my-policy`. The
plugin never sees an unverified token; it receives the `Principal` and a
context of `operation`, `method`, `route` and `path_params`. Returning
`False` denies; raising `ServiceError` reports that no decision could be
reached.

## Behaviour summary

| Request | Auth disabled | Auth enabled |
| --- | --- | --- |
| `GET`, or any query | 200 | 200 (token verified if present) |
| Mutating call, no token | 200 | `401` + `WWW-Authenticate: Bearer` |
| Mutating call, forged/expired token | 200 | `401` |
| Mutating call, valid token without the role | 200 | `403` |
| Mutating call, valid token with the role | 200 | 200 |
| Any call, unverifiable token | ignored | `401` |

## Not covered yet

Job ownership (`ownerId`), per-user job visibility and token exchange for
downstream SRCNet calls remain open — see package 4 in the
[roadmap](roadmap.md). Deletion audit records currently name the deleted
document but not the subject that deleted it.
