# ObsCore

Deployments running the `odp` metadata plugin publish
[ObsCore 1.1](https://www.ivoa.net/documents/ObsCore/) — the IVOA's common
model for observational datasets — as the view `ivoa.obscore`, one row per
ingested data product. Any ObsTAP-aware client (pyvo's
`TAPService.search`, TOPCAT's ObsTAP window, Aladin) discovers it through
the declared data model:

```xml
<dataModel ivo-id="ivo://ivoa.net/std/ObsCore#core-1.1">ObsCore-1.1</dataModel>
```

Nothing is ingested twice: the SRCNet ODP model is itself ObsCore-derived,
so the view is a projection of the `srcnet` tables, created by the plugin's
bootstrap exactly when its source tables are (and replaced at startup when
the mapping changed, so a change migrates forward — see [Migrating the
view](#migrating-the-view)). All 30 mandatory columns of REC Table 6 are
present with their standard units, UCDs and utypes.

## The column mapping

| ObsCore column | comes from |
| --- | --- |
| `dataproduct_type` | `data_products.dataproduct_type`, with the srcnet-only value `'table'` mapped to `'measurements'` (the ObsCore vocabulary has no `table`) |
| `calib_level` | `data_products.calib_level`, passed through untranslated — see [Calibration levels](#calibration-levels) |
| `obs_collection` | `observations.collection`, `'unclassified'` when absent (the REC requires a value) |
| `obs_id` | `data_products.obs_id` |
| `obs_publisher_did` | constructed — see below |
| `access_url`, `access_format`, `access_estsize` | one representative artifact per product: the first `science`-semantics artifact by id, picked by an aggregate so the planner can drop the join from queries that read none of the three. `access_estsize` converts the model's bytes to the REC's kbyte. A product with no science artifact has NULL access columns, which the spec permits |
| `target_name`, `s_ra`, `s_dec`, `s_fov`, `s_region`, `s_xel1`, `s_xel2`, `t_min`, `t_max`, `t_exptime`, `t_xel`, `em_min`, `em_max`, `em_xel`, `o_ucd`, `pol_states`, `pol_xel` | the identically-named `data_products` columns |
| `s_resolution` | `data_products.beam_size` (already arcseconds) |
| `t_resolution`, `em_res_power` | NULL — the model does not carry them, and NULL is permitted |
| `facility_name`, `instrument_name` | the identically-named `observations` columns |
| `s_region_geom` *(non-standard, `std = 0`)* | the pgsphere footprint derived from `s_region` at ingestion — see [Metadata plugins](plugins.md#querying-footprints-s_region) |

The extra `s_region_geom` column is what makes footprint queries work
directly on the view:

```sql
SELECT obs_publisher_did, dataproduct_type, access_url
FROM ivoa.obscore
WHERE 1 = INTERSECTS(s_region_geom, CIRCLE('ICRS', 150.0, -30.0, 0.5))
```

## The publisher DID

`obs_publisher_did` is a configurable prefix plus the product's full
identity chain:

```
<prefix><project_id>/<obs_id>/<sbd_id>/<eb_id>/<product_id>
```

The key columns are free `text` in the data model, so each one is
percent-encoded into the path: everything outside the RFC 3986 unreserved
set (`A-Za-z0-9._~-`) becomes `%XX` per UTF-8 byte. A `product_id` of
`cube 3/a` therefore appears as `cube%203%2Fa` rather than forging a sixth
path segment, and two products can never collide on one identifier. The
`/` separators between components are the only unencoded ones.

The encoder is the SQL function `ivoa.did_encode(text)`, `IMMUTABLE` and
`PARALLEL SAFE`, and a component that is already clean never calls it. That
keeps the DID expression free of subqueries, which is what lets the
bootstrap index it: `data_products_obscore_did_trgm` is a pg_trgm GIN index
on `srcnet.data_products` over exactly the view's expression, so a lookup
by DID — equality, a prefix, or a component in the middle such as
`obs_publisher_did LIKE '%<project_id>/%'` — is an index scan rather than
an evaluation of the expression over every product. `pg_trgm` is a trusted
extension, so the database owner can install it without being superuser; a
bootstrap role that cannot logs a warning and leaves the view without its
index. The function, the view and the index are fingerprinted together, so
a changed prefix or chain rebuilds all three at the next bootstrap.

### Choosing the chain

Which columns form the chain is set with `config.obscoreDidColumns` (env
`TAP_OBSCORE_DID_COLUMNS`), dot-separated and in order:

```yaml
config:
  obscoreDidColumns: "project_id.obs_id.sbd_id.eb_id.product_id"
```

The default is the **primary key of `srcnet.data_products`** — one
component per level of the ODP hierarchy — because that is exactly what
identifies a data product uniquely. Set it only for a data model that nests
differently, and with the same care the prefix deserves:

- a chain that does **not** identify a product uniquely makes two products
  share a DID, which the percent-encoding cannot save you from;
- changing it re-identifies every dataset already published.

Each component must be a plain lower-case SQL identifier; the names are
interpolated into the view definition, where nothing can be bound, so
anything else is refused before it reaches a `CREATE VIEW`. A name that is
syntactically fine but absent from `srcnet.data_products` fails at
bootstrap, where PostgreSQL names it.

The prefix defaults to `ivo://skao.int/~?` and is set with
`config.obscoreDidPrefix` (env `TAP_OBSCORE_DID_PREFIX`). A PublisherDID is
a **permanent promise**: set the authority to match your registry
`authorityId` before publishing anything real, because changing the prefix
re-identifies every dataset ever served. The prefix is validated against
the IVOA identifier alphabet before it is interpolated into the view
definition.

## Calibration levels

`calib_level` is the one column where SRCNet and ObsCore 1.1 do not agree,
and the view **passes the value through untranslated**:

| level | srcnet ([model schemas](model-schemas.md)) | ObsCore 1.1 |
| --- | --- | --- |
| 0 | raw | raw — instrumental data in a proprietary format |
| 1 | calibrated | instrumental data in a standard format (FITS, VOTable) |
| 2 | science-ready | calibrated, science-ready, instrument signature removed |
| 3 | analysis | enhanced data products (mosaics, source catalogues, cubes) |

Levels 0 and 3 line up. Level 1 does not: srcnet calls it *calibrated*,
where ObsCore reserves that word for level 2 and reads level 1 as merely
*format-converted*. Level 2 is the knock-on — srcnet's "science-ready"
matches ObsCore's level-2 wording, which is only reachable by shifting
level 1 first.

Nothing is remapped here on purpose. `dataproduct_type` **did** get a `CASE`
mapping in the view (srcnet's `'table'` becomes ObsCore's `'measurements'`)
because that is a vocabulary rename with one right answer. Relabelling a
calibration level is not: it asserts something about how the data were
processed, and only the data model owns that claim. So the view reports what
the producer declared, and the TAP_SCHEMA description for the column says as
much rather than reciting ObsCore's vocabulary over values that do not
follow it.

Consequence for clients: a `calib_level >= 2` filter over `ivoa.obscore`
selects srcnet's science-ready and analysis products, which is close to but
not the same as ObsCore's intent. This stays an open item pending a
data-model decision on which side moves.

## Migrating the view

The view definition is fingerprinted into the view's own comment, and the
bootstrap compares that fingerprint before it does anything. A pod start
that is not a mapping change therefore issues no view DDL at all — which
matters because both `DROP VIEW` + `CREATE VIEW` and `CREATE OR REPLACE
VIEW` take an `ACCESS EXCLUSIVE` lock on `ivoa.obscore` and hold it until
the bootstrap transaction commits, so one long-running ObsCore query plus a
rolling deploy would otherwise queue every new query behind each pod's DDL
in turn.

When the definition did change, the bootstrap tries `CREATE OR REPLACE VIEW`
first — it keeps the relation's OID and its grants — and falls back to
`DROP VIEW` + `CREATE VIEW` only when Postgres refuses the replacement
because the column list itself moved (SQLSTATE 42P16). The fallback runs
after rolling back to a savepoint, since the refused statement has aborted
the transaction.

An `ivoa.obscore` that already exists and is *not* a view is left alone
entirely: that deployment is publishing its own ObsCore, and neither
destroying it nor crashing the bootstrap on it is acceptable.

## What a deployment without `odp` looks like

The view, its TAP_SCHEMA registration, the `<dataModel>` declaration in
`/tap/capabilities` and the registry record, and the ObsCore entry in
`/tap/examples` all key off the same condition — the `odp` plugin being
active — so the REC's rule that the data model may only be declared once
the table with all mandatory columns exists holds structurally. A
deployment running only the `software` plugin declares nothing and serves
no `ivoa` schema.
