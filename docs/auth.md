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

The two are configured separately, because the answers differ. By default
**every request needs a verified token** (`auth.requireToken`) except service
discovery and the health check. Only some of those requests additionally need
an authorisation *decision* — see [what is gated](#what-is-gated). So a
metadata `GET` needs a token and nothing more; deleting a metadata document
needs a token the plugin approves.

Reading metadata through TAP — `/tap/sync` and the `/tap/async` job — can be
reopened to token-less callers with `auth.anonymousQueries`. That switch is
what decides whether standard VO clients can use the service at all.

The environment variables behind these switches are read strictly: `1`,
`true`, `yes`, `on`, `0`, `false`, `no`, `off`, in any case, and anything else
refuses to start. Mapping an unrecognised value to false is how
`TAP_AUTH_REQUIRE_TOKEN=flase` would turn the token requirement off without
saying so.

## Getting a token: the AuthVO challenge

A bare `401` leaves a client guessing which IAM to talk to. A request refused
for want of a token is answered with an IVOA AuthVO challenge naming the
OIDC discovery document instead — the same shape the SRCNet
[DM product streamer](https://gitlab.com/ska-telescope/src/src-dm/ska-src-dm-product-streamer-api)
uses, so a client written against one works against the other:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer realm="skao-tap",
  ivoa_bearer error="invalid_request",
  error_description="Missing access token",
  discovery_url="https://ska-iam.stfc.ac.uk/.well-known/openid-configuration"
```

The RFC 6750 `Bearer` challenge comes first so a client reading only the
first one still learns it needs a bearer token; the `ivoa_bearer` challenge
carries the discovery URL. The same text is repeated in the error body — the
JSON `message` on `/api/v1/*`, the DALI VOTable on `/tap/*` — because that is
where the reference SRCNet client reads it from.

A client then runs OIDC discovery and a device flow against the IAM, and
comes back with the token. The service never talks to the IAM on the client's
behalf; it only says where the IAM is.

Two error codes are distinguished, because the remedy differs:

| `error` | Means | What the client should do |
| --- | --- | --- |
| `invalid_request` | no credential was presented | discover the IAM, get a token |
| `invalid_token` | a credential was presented and did not verify (expired, forged, wrong issuer) | get a *fresh* token; re-running discovery will not help |

`GET /api/v1/auth` advertises the same `discovery_url`, so a client can find
the IAM without provoking a `401` first.

Audience: this service does not require tokens to carry a service-specific
`aud`, provided `auth.iam.allowAnyAudience` is set — SRCNet IAM tokens are
not minted per service here. That is a deliberate trade: any token the IAM
issued to any of its clients is then accepted here, so the flag has to be set
explicitly rather than defaulted. No token-exchange step is involved.

## What needs a token

| Requests | Token | Why |
| --- | --- | --- |
| `/tap/availability` | no | a Kubernetes probe cannot hold one |
| `/tap/capabilities`, `/tap/tables`, `/tap/registry`, `/tap/examples` | no | a registry harvester or a VO client browsing for services cannot hold one |
| `/api/v1/auth`, `/openapi.json`, `/docs` | no | this is where a client works out how to authenticate |
| `GET /tap/sync?REQUEST=getCapabilities` | no | TAP 1.0 capability discovery: the handler redirects it to the open `/capabilities`, so a token would guard nothing |
| `/tap/sync`, `/tap/async` and its sub-resources | **configurable** | `auth.anonymousQueries` — off by default |
| everything else — `/api/v1/<mount>` reads and writes, `/api/v1/query`, `/api/v1/jobs` | yes | a JSON client can authenticate, and is expected to |

The capability-discovery exemption is deliberately narrower than it could be:
`GET` only, compared exactly as the handler compares it. `gather_params`
merges a POST form *over* the query string, so on a `POST` the query string
does not decide what the handler does — and reading the body here to find out
would consume it before the handler could. So `POST /tap/sync` with
`REQUEST=getCapabilities` needs a token, while the `GET` form does not. An
exemption wider than the redirect it exists for would be a way past the token
requirement, not a convenience.

### Serving standard VO clients

```yaml
auth:
  enabled: true
  anonymousQueries: true    # PyVO, TOPCAT and friends carry no token
```

PyVO, TOPCAT and the rest of the VO toolchain have no way to obtain or send a
bearer token, so with `requireToken: true` and `anonymousQueries: false` they
get `401` on every query. Turning `anonymousQueries` on opens exactly two
things: a synchronous query, and the UWS job that runs one — including the
job's sub-resources (`/phase`, `/parameters`, `/results`, …), because they are
branches of the same read.

Two qualifications, because three settings decide this between them:

- `requireToken: false` makes those paths anonymous whatever
  `anonymousQueries` says — it turns the whole authentication layer off.
- `anonymousQueries: true` only settles the *authentication* layer. If
  `gatedOperations` enforces the query operations, the authorisation layer
  still refuses a caller whose token the plugin does not approve — and a
  token-less caller has no token to approve, so it gets `401` there instead.

`GET /api/v1/auth` reports the combined answer as `anonymous_tap_queries`, so
a client does not have to work this out.

It does **not** open the JSON API's own query and job facades
(`/api/v1/query`, `/api/v1/jobs`). The switch exists for clients that cannot
authenticate; a JSON client can.

That leaves a deliberate asymmetry when it is on: the same metadata is
readable anonymously with an ADQL query over `/tap/sync` and token-gated when
read as JSON from `/api/v1/<mount>`. What justifies it is the client
population, not the data. Ownership still applies either way — an anonymous
caller sees jobs with no owner and gets `403` on a job someone claimed.

Two further notes for a deployment that opens queries:

- Anonymous queries reach whatever the query role can read, so what protects
  unpublished data is TAP_SCHEMA publication and the `tap_reader` grants, not
  this switch.
- The gated operations still apply on top. Adding `query.sync` and the
  `jobs.*` operations to `auth.gatedOperations` re-closes the query surface
  for callers whose token the plugin does not approve, which is a different
  question from whether a token is needed at all.

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

The default enforces metadata mutation only. That is about *authorisation* —
which requests need a decision about the token they carry — and it is a
separate question from whether a token is needed at all, which
[`auth.requireToken` and `auth.anonymousQueries`](#what-needs-a-token) answer.
Gating "all writes" by HTTP method would be no help here anyway: TAP clients
send ADQL as a POST body, so what the gate set protects is the data a caller
can *change*, not the verb they used.

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

The four query operations are enforced **as a group**: naming some but not
all of them is refused, by the chart at render time and by the service at
startup. A subset is not a weaker policy, it is an incoherent one — a caller
refused at `POST /tap/async` runs the same query at `/tap/sync`, and gating
job mutation without job creation hands a VO client a job it cannot start.

One thing to know before enforcing them: **anonymous VO clients stop
working.** A client that sends no token gets `401` on `POST /tap/sync` and
`POST /tap/async`, which is how PyVO and TOPCAT submit queries. That is the
intended effect, not a side effect.

The metadata operations stay independent of each other — ingest, amendment
and deletion are separately grantable, and typically separately granted.

### What a client can still do without a token

No read is gated by *this* setting, whatever is enforced — but a read still
needs a token unless the request is one `auth.anonymousQueries` opens or a
discovery endpoint. Where reads are reachable, what limits them is
[ownership](#job-ownership): an anonymous caller sees jobs with no owner, and
gets `403` on a job someone claimed. So with `anonymousQueries: true` and the
query operations enforced, an anonymous TOPCAT cannot create or start a job,
and cannot see any job created with a token — but it can still read a job
that was created anonymously, if any exist.

With `plugin: iam-groups`, every operation listed in `gatedOperations` must
also be granted under `roles` — an enforced operation nobody is granted is
denied to everyone, so the chart refuses to render that configuration rather
than shipping a service that answers `403` to its own operators.

No `GET` needs an authorisation decision, with one exception:
`GET /tap/sync` executes a query rather than reading a resource, so
`query.sync` covers it in both verbs. Needing a *token*, though, is decided
elsewhere — the VOSI documents never do, job and metadata reads do unless
`anonymousQueries` opens the TAP ones. What stops one user reading another
user's job is [ownership](#job-ownership), enforced in the job store rather
than at the endpoint.

To verify tokens and record job ownership while enforcing nothing, set
`gatedOperations: ["none"]`. That is the only way to say it: a list that
names no operation is rejected, so a typo cannot quietly turn the gate off.

`GET /api/v1/auth` reports what the deployment enforces, so clients need not
discover it by trial:

```json
{"enabled": true, "plugin": "permissions-api",
 "token_required": true, "anonymous_queries": false,
 "anonymous_tap_queries": false,
 "discovery_url": "https://ska-iam.stfc.ac.uk/.well-known/openid-configuration",
 "issuer": "https://ska-iam.stfc.ac.uk", "audience": "science-metadata",
 "gated_operations": {"metadata.ingest": "POST /api/v1/<mount>", ...}}
```

`token_required` and `anonymous_queries` describe the authentication layer;
`gated_operations` answers "which requests need my token to be *approved*?".
A client that just wants to know whether it can query without a token reads
**`anonymous_tap_queries`**, which combines all three — it is false when a
token is required and the TAP paths are not reopened, and also false when
they are reopened but the query operations are gated.

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

`gated_operations` lists only the operations this deployment puts to the
plugin for a decision, and it is empty when authentication is disabled,
because then nothing is enforced whatever the gate set says. It does not on
its own say whether a token is needed — `token_required` and
`anonymous_queries` in the same response answer that.

"Gated call" below means a request covered by an operation this deployment
enforces — that is the authorisation layer. The rows above it are the
authentication layer, which applies whether or not an operation is gated.

The three metadata operations are enforced independently of one another; the
four query operations are enforced as a group, so "enforcing `query.sync`"
always means the whole query surface is enforced.

| Request | Auth disabled | Auth enabled |
| --- | --- | --- |
| Discovery or health endpoint, no token | 200 | 200 |
| TAP query or job, no token, `anonymousQueries: true`, query operations ungated | 200 | 200 |
| TAP query or job, no token, `anonymousQueries: false` | 200 | `401` + AuthVO challenge |
| `GET /tap/sync?REQUEST=getCapabilities`, no token | 303 | 303 — discovery, not a query, whatever is configured |
| The same by `POST`, no token | 303 | `401` — the exemption is the `GET` form only (see below) |
| Any other request, no token | 200 | `401` + AuthVO challenge |
| Any request, forged/expired token | ignored | `401`, `error="invalid_token"` |
| Metadata `GET`, valid token | 200 | 200 (no role needed) |
| Gated call, no token | 200 | `401` + AuthVO challenge |
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
