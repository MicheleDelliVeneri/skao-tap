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

Seven operations can be gated. Which ones a deployment actually enforces is
its own choice, set with `auth.gatedOperations`:

| Operation | Requests | Enforced by default |
| --- | --- | --- |
| `metadata.ingest` | `POST /api/v1/<mount>` | yes |
| `metadata.amend` | `PATCH /api/v1/<mount>/{root_id}` | yes |
| `metadata.delete` | `DELETE /api/v1/<mount>/{root_id}` | yes |
| `jobs.create` | `POST /tap/async`, `POST /api/v1/jobs` | no |
| `jobs.mutate` | `POST /tap/async/{job_id}/{phase,executionduration,destruction,parameters}`, `POST /api/v1/jobs/{job_id}/phase` | no |
| `jobs.delete` | `DELETE /tap/async/{job_id}`, `POST /tap/async/{job_id}` with `ACTION=DELETE`, `DELETE /api/v1/jobs/{job_id}` | no |
| `query.sync` | `GET`/`POST /tap/sync`, `POST /api/v1/query` | no |

The default enforces metadata mutation only, and every `GET` stays open
whatever is configured. That default is deliberate: TAP clients send ADQL as
a POST body, so gating "all writes" by HTTP method would lock PyVO, TOPCAT
and every other standard VO client out of an authenticated deployment. What
is protected by default is the data a caller can *change*.

### Requiring tokens for querying

A site where every client is expected to authenticate can enforce the job
and query operations too:

```yaml
auth:
  enabled: true
  gatedOperations:
    - metadata.ingest
    - metadata.amend
    - metadata.delete
    - jobs.create
    - jobs.mutate
    - jobs.delete
    - query.sync
  roles:
    # … a grant for each operation listed above
```

Two things to know before doing that:

- **Anonymous VO clients stop working.** A client that sends no token gets
  `401` on `POST /tap/sync` and `POST /tap/async`, which is how PyVO and
  TOPCAT submit queries. This is the intended effect, not a side effect.
- **`jobs.create` alone is not a closed door.** Synchronous querying reaches
  the same data without creating a job, so enforcing `jobs.create` while
  leaving `query.sync` open only changes which endpoint an anonymous caller
  uses. Enforce the two together.

With `plugin: iam-groups`, every operation listed in `gatedOperations` must
also be granted under `roles` — an enforced operation nobody is granted is
denied to everyone, so the chart refuses to render that configuration rather
than shipping a service that answers `403` to its own operators.

Reads are never gated by this setting. What stops one user reading another
user's job is [ownership](#job-ownership), which is enforced in the job
store, not at the endpoint.

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

An **audience is required**. One IAM issues tokens to many services, so
without `aud` validation a token minted for any other client of the same
issuer would be accepted here as its bearer's credential. Accepting that is
possible but has to be asked for: `auth.iam.allowAnyAudience: true`.

Group membership is read from `groups` and `wlcg.groups` by default, and
normalised to a leading slash, so `/ska/oper` and `ska/oper` in
configuration mean the same thing. `entitlements` and
`eduperson_entitlement` are **not** read: a federated home IdP can assert
those, and treating them as group names would let an attribute asserted
elsewhere match a local policy group. A deployment that trusts them can add
them with `auth.iam.groupClaims`.

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

Every operation must be granted explicitly. An operation left out of the
mapping, **or present with neither groups nor scopes, is denied** — an
unfinished policy grants nothing, least of all deletion. Accepting any
verified token is a choice that has to be written down:

```yaml
    metadata.amend:
      anyVerifiedToken: true
```

The chart refuses to render `plugin: iam-groups` with an empty `roles`
mapping, so enabling auth without writing a policy fails at deploy time
rather than quietly denying (or, worse, quietly allowing) every write.

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

!!! warning "The token travels in the query string"
    The Permissions API's contract takes the access token as a **required
    query parameter** (`?token=<jwt>`), not a header — its OpenAPI document
    declares no header credential, so this cannot be avoided from the client
    side. Query strings are recorded by access logs, ingress controllers and
    tracing systems, so live tokens may be readable wherever those logs land.
    Treat the Permissions API's access logs as credential material, or use
    the `iam-groups` plugin, which never forwards the token anywhere.

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

`gated_operations` lists only what this deployment enforces, so a client can
tell from it whether it needs a token to query at all.

| Request | Auth disabled | Auth enabled |
| --- | --- | --- |
| `GET` | 200 | 200 (token verified if present) |
| Query, `query.sync`/`jobs.create` not enforced | 200 | 200 (token verified if present) |
| Query, `query.sync`/`jobs.create` enforced, no token | 200 | `401` |
| Gated call, no token | 200 | `401` + `WWW-Authenticate: Bearer` |
| Gated call, forged/expired token | 200 | `401` |
| Gated call, valid token without the role | 200 | `403` |
| Gated call, valid token with the role | 200 | 200 |
| Any call, unverifiable token | ignored | `401` |

## Job ownership

When auth is enabled, a job created by an identified caller records that
caller's subject as its UWS `ownerId`, and becomes private to them:

| | Owned by you | Owned by someone else | Created anonymously |
| --- | --- | --- | --- |
| Appears in the job list | yes | no | yes |
| Readable, abortable, deletable | yes | `403` | yes |

A job created without a token stays ownerless and behaves exactly as it
always has — there is no identity to protect it with, and querying is
anonymous by design. Jobs are only private once someone has claimed them.

Ownership is enforced in the job store rather than at each endpoint, so it
holds for every route that can reach a job: the UWS resources and all their
sub-resources (`/phase`, `/parameters`, `/results`, …), the JSON facade, and
the result download. The executor, which has no request and must see every
job, is unaffected.

One consequence worth knowing: `POST /tap/async` answers `303` pointing at
the job, and an HTTP client only replays its `Authorization` header on a
redirect within the same origin. Set `tapApi.baseUrl` to the URL clients
actually reach the service on, or a redirect-following client will arrive at
its own job unauthenticated and get `403`.

Deletions name the subject responsible:

```
INFO tapcore deleted srcnet.software 'ska:demo:1.0.0' by 'a4f1…' (cascading to 1 descendant table(s))
```

## Not covered yet

Token exchange for downstream SRCNet calls, and per-user database schemas or
quotas — see package 4 in the [roadmap](roadmap.md).
