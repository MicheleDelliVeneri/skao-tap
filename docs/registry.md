# VO Registry publication

A TAP service that nobody can find is a private database with an IVOA
interface. Registration is what makes it discoverable: a **VOResource**
record describing the service is published to a *publishing registry*, which
the VO Registry harvests, so PyVO's `registry.search(servicetype="tap")`,
TOPCAT's service browser and RegTAP queries can all find it.

This service generates its own record and serves it at `GET /tap/registry`.

## Why generated rather than a file

The record has to state what the service supports — ADQL version, output
formats, upload methods, limits, endpoint URLs. All of that already exists in
`/tap/capabilities`, and it changes with configuration: `config.waitMaxSeconds`,
`config.jobRetentionSeconds`, the MAXREC limits, `tapApi.baseUrl`. A record
kept as a file beside the service starts as a copy and ends as a claim about
a service that no longer exists.

So `/tap/registry` reuses the very same capability elements
`/tap/capabilities` serves, and takes the identity metadata from the chart.
A record that disagrees with the service it describes is worse than no
record: harvesters cache it, and clients trust it.

## Turning it on

Off by default, and nothing is defaulted. An IVOA identifier is a permanent
promise that `ivo://<authorityId>/<resourceKey>` resolves to *this* service,
so the values can only come from whoever holds the authority:

```yaml
voRegistry:
  enabled: true
  authorityId: skao.int          # an authority you may publish under
  resourceKey: srcnet/tap        # path within it
  title: SKAO SRCNet TAP service
  shortName: SKAO TAP            # at most 16 characters
  description: >-
    Science metadata and data products from SKA Regional Centres,
    queryable with ADQL over TAP 1.1.
  referenceUrl: https://srcnet.example.org/tap
  publisher: SKA Observatory
  creator: SKA Observatory       # optional
  contact:                       # optional; a team alias, not a person
    name: SRCNet operations
    email: srcnet-support@example.org
  subjects:                      # at least one
    - radio astronomy
    - surveys
  types: ["Archive"]
  contentLevels: ["Research"]
  created: "2026-08-23"          # ISO-8601; `updated` defaults to this
```

The chart refuses to render `enabled: true` with any required value unset, or
with a `shortName` over the VOResource limit — a record that a registry will
reject should fail at deploy time, not in someone else's ingest log a week
later. The service raises the same errors, naming the Helm value to edit, for
deployments configured through environment variables directly.

With it off, `GET /tap/registry` answers `404`. Every other endpoint is
unaffected either way.

## Publishing the record

Generating the record is not publishing it. Nothing here contacts a registry
on its own — a service that registered itself on startup would be a service
that re-registers itself on every rollback.

The manual step, once per deployment:

1. Check the record renders as you expect:
   `curl -s https://<host>/tap/registry`.
2. Validate it against the VOResource and TAPRegExt schemas — the IVOA
   validator, or `xmllint --schema`.
3. Submit it to a publishing registry that accepts records for your
   authority. SKAO publishes under its own; other sites commonly use their
   national or institutional publishing registry.
4. Confirm it appears in a RegTAP query against
   `ivo://ivoa.net/std/RegTAP#Registry-1.0`, e.g. in PyVO:

   ```python
   import pyvo

   pyvo.registry.search(ivoid="ivo://skao.int/srcnet/tap")
   ```

Update the record whenever the identity metadata or the service's
capabilities change, and bump `voRegistry.updated`: harvesters order
revisions by it, so a changed record with an unchanged `updated` date can be
ignored.

## What the record contains

| Element | Source |
| --- | --- |
| `identifier` | `voRegistry.authorityId` + `resourceKey` |
| `title`, `shortName`, `content/description`, `content/referenceURL` | chart values |
| `curation/publisher`, `creator`, `contact` | chart values (`creator` and `contact` optional) |
| `content/subject`, `type`, `contentLevel` | chart values |
| `created`, `updated`, `status` | chart values; `status` is always `active` |
| `capability` (TAP + the three VOSI ones) | the live service, same as `/tap/capabilities` |

The record is typed `vs:CatalogService`, which is what a TAP service with a
queryable tableset is.

## Not covered

An OAI-PMH interface, so a publishing registry could harvest this service
directly instead of being handed the record once. Worth adding if a registry
you need asks for it; most accept a submitted document or a URL.
