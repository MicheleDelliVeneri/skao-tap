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
bootstrap exactly when its source tables are (and dropped-and-recreated at
startup, so a mapping change migrates forward). All 30 mandatory columns of
REC Table 6 are present with their standard units, UCDs and utypes.

## The column mapping

| ObsCore column | comes from |
| --- | --- |
| `dataproduct_type` | `data_products.dataproduct_type`, with the srcnet-only value `'table'` mapped to `'measurements'` (the ObsCore vocabulary has no `table`) |
| `calib_level` | `data_products.calib_level` |
| `obs_collection` | `observations.collection`, `'unclassified'` when absent (the REC requires a value) |
| `obs_id` | `data_products.obs_id` |
| `obs_publisher_did` | constructed — see below |
| `access_url`, `access_format`, `access_estsize` | one representative artifact per product: the first `science`-semantics artifact by id. `access_estsize` converts the model's bytes to the REC's kbyte. A product with no science artifact has NULL access columns, which the spec permits |
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

The prefix defaults to `ivo://skao.int/~?` and is set with
`config.obscoreDidPrefix` (env `TAP_OBSCORE_DID_PREFIX`). A PublisherDID is
a **permanent promise**: set the authority to match your registry
`authorityId` before publishing anything real, because changing the prefix
re-identifies every dataset ever served. The prefix is validated against
the IVOA identifier alphabet before it is interpolated into the view
definition.

## What a deployment without `odp` looks like

The view, its TAP_SCHEMA registration, the `<dataModel>` declaration in
`/tap/capabilities` and the registry record, and the ObsCore entry in
`/tap/examples` all key off the same condition — the `odp` plugin being
active — so the REC's rule that the data model may only be declared once
the table with all mandatory columns exists holds structurally. A
deployment running only the `software` plugin declares nothing and serves
no `ivoa` schema.
