"""ObsCore 1.1 (`ivoa.obscore`) over the ODP metadata.

The SRCNet ODP model is ObsCore-derived, so compliance is a *view*, not a
table: `srcnet.data_products` already carries most of the mandatory columns
under their exact ObsCore names, `srcnet.observations` has the collection
and provenance names, and `srcnet.artifacts` the access columns. The view
is created by the odp plugin's bootstrap (`MetadataPlugin.post_ensure`), so
it exists exactly when its source tables do, and replaced on every startup
so a mapping change migrates forward.

Column metadata is transcribed from REC-ObsCore-v1.1-20170509 Table 6 (the
TAP_SCHEMA values for the mandatory fields); utypes carry the ``obscore:``
prefix the table's caption says it omits. One non-standard column rides
along: ``s_region_geom``, the pgsphere footprint the ingest pipeline
derives from ``s_region`` (package 7), registered with ``std = 0`` so ADQL
``INTERSECTS``/``CONTAINS`` work on the view too.

Mapping decisions (each visible in the SQL below):

- ``dataproduct_type = 'table'`` is in the srcnet CHECK but not the ObsCore
  vocabulary; it maps to ``'measurements'``.
- ``obs_collection`` honours the REC's NOT NULL with
  ``COALESCE(collection, 'unclassified')``.
- ``obs_publisher_did`` is a configurable prefix (``TAP_OBSCORE_DID_PREFIX``)
  plus the primary-key chain — a DID must be permanent, so its shape is the
  hierarchy's identity and nothing derived — with each key component
  percent-encoded (see ``_did_component``).
- ``calib_level`` is passed through untranslated; srcnet's declared meaning
  and ObsCore 1.1's disagree at level 1 (see ``docs/obscore.md``).
- ``access_*`` come from one representative science artifact per product
  (`LEFT JOIN LATERAL ... LIMIT 1`); a NULL ``access_url`` is spec-legal.
- ``access_estsize`` converts the model's bytes to the REC's kbyte.
- ``s_resolution`` is the synthesized beam size (already arcseconds).
- ``t_resolution`` and ``em_res_power`` are NULL: the model does not carry
  them, and NULL is permitted.
"""

import hashlib
import logging
import re

from egernia_core.config import settings

log = logging.getLogger("tap-api")

# (name, datatype, arraysize, xtype, unit, ucd, utype, principal, std, select expression)
# in REC Table 6 order; datatypes are the VOTable names TAP_SCHEMA uses.
OBSCORE_COLUMNS: list[tuple] = [
    (
        "dataproduct_type",
        "char",
        "*",
        None,
        None,
        "meta.id",
        "obscore:ObsDataset.dataProductType",
        1,
        1,
        "CASE p.dataproduct_type WHEN 'table' THEN 'measurements' ELSE p.dataproduct_type END",
    ),
    (
        "calib_level",
        "int",
        None,
        None,
        None,
        "meta.code;obs.calib",
        "obscore:ObsDataset.calibLevel",
        1,
        1,
        "p.calib_level::integer",
    ),
    (
        "obs_collection",
        "char",
        "*",
        None,
        None,
        "meta.id",
        "obscore:DataID.collection",
        1,
        1,
        "COALESCE(o.collection, 'unclassified')",
    ),
    (
        "obs_id",
        "char",
        "*",
        None,
        None,
        "meta.id",
        "obscore:DataID.observationID",
        1,
        1,
        "p.obs_id",
    ),
    (
        "obs_publisher_did",
        "char",
        "*",
        None,
        None,
        "meta.ref.uri;meta.curation",
        "obscore:Curation.publisherDID",
        1,
        1,
        None,  # built from the DID prefix at view-creation time
    ),
    (
        "access_url",
        "char",
        "*",
        None,
        None,
        "meta.ref.url",
        "obscore:Access.reference",
        1,
        1,
        "a.access_url",
    ),
    (
        "access_format",
        "char",
        "*",
        None,
        None,
        "meta.code.mime",
        "obscore:Access.format",
        1,
        1,
        "a.access_format",
    ),
    (
        "access_estsize",
        "long",
        None,
        None,
        "kbyte",
        "phys.size;meta.file",
        "obscore:Access.size",
        1,
        1,
        "round(a.access_estsize / 1000.0)::bigint",
    ),
    (
        "target_name",
        "char",
        "*",
        None,
        None,
        "meta.id;src",
        "obscore:Target.name",
        1,
        1,
        "p.target_name",
    ),
    (
        "s_ra",
        "double",
        None,
        None,
        "deg",
        "pos.eq.ra",
        "obscore:Char.SpatialAxis.Coverage.Location.Coord.Position2D.Value2.C1",
        1,
        1,
        "p.s_ra",
    ),
    (
        "s_dec",
        "double",
        None,
        None,
        "deg",
        "pos.eq.dec",
        "obscore:Char.SpatialAxis.Coverage.Location.Coord.Position2D.Value2.C2",
        1,
        1,
        "p.s_dec",
    ),
    (
        "s_fov",
        "double",
        None,
        None,
        "deg",
        "phys.angSize;instr.fov",
        "obscore:Char.SpatialAxis.Coverage.Bounds.Extent.diameter",
        1,
        1,
        "p.s_fov",
    ),
    (
        "s_region",
        "char",
        "*",
        "adql:REGION",
        None,
        "pos.outline;obs.field",
        "obscore:Char.SpatialAxis.Coverage.Support.Area",
        1,
        1,
        "p.s_region",
    ),
    (
        "s_resolution",
        "double",
        None,
        None,
        "arcsec",
        "pos.angResolution",
        "obscore:Char.SpatialAxis.Resolution.Refval.value",
        1,
        1,
        "p.beam_size",
    ),
    (
        "s_xel1",
        "long",
        None,
        None,
        None,
        "meta.number",
        "obscore:Char.SpatialAxis.numBins1",
        1,
        1,
        "p.s_xel1",
    ),
    (
        "s_xel2",
        "long",
        None,
        None,
        None,
        "meta.number",
        "obscore:Char.SpatialAxis.numBins2",
        1,
        1,
        "p.s_xel2",
    ),
    (
        "t_min",
        "double",
        None,
        None,
        "d",
        "time.start;obs.exposure",
        "obscore:Char.TimeAxis.Coverage.Bounds.Limits.StartTime",
        1,
        1,
        "p.t_min",
    ),
    (
        "t_max",
        "double",
        None,
        None,
        "d",
        "time.end;obs.exposure",
        "obscore:Char.TimeAxis.Coverage.Bounds.Limits.StopTime",
        1,
        1,
        "p.t_max",
    ),
    (
        "t_exptime",
        "double",
        None,
        None,
        "s",
        "time.duration;obs.exposure",
        "obscore:Char.TimeAxis.Coverage.Support.Extent",
        1,
        1,
        "p.t_exptime",
    ),
    (
        "t_resolution",
        "double",
        None,
        None,
        "s",
        "time.resolution",
        "obscore:Char.TimeAxis.Resolution.Refval.value",
        1,
        1,
        "NULL::double precision",
    ),
    (
        "t_xel",
        "long",
        None,
        None,
        None,
        "meta.number",
        "obscore:Char.TimeAxis.numBins",
        1,
        1,
        "p.t_xel",
    ),
    (
        "em_min",
        "double",
        None,
        None,
        "m",
        "em.wl;stat.min",
        "obscore:Char.SpectralAxis.Coverage.Bounds.Limits.LoLimit",
        1,
        1,
        "p.em_min",
    ),
    (
        "em_max",
        "double",
        None,
        None,
        "m",
        "em.wl;stat.max",
        "obscore:Char.SpectralAxis.Coverage.Bounds.Limits.HiLimit",
        1,
        1,
        "p.em_max",
    ),
    (
        "em_res_power",
        "double",
        None,
        None,
        None,
        "spect.resolution",
        "obscore:Char.SpectralAxis.Resolution.ResolPower.refVal",
        1,
        1,
        "NULL::double precision",
    ),
    (
        "em_xel",
        "long",
        None,
        None,
        None,
        "meta.number",
        "obscore:Char.SpectralAxis.numBins",
        1,
        1,
        "p.em_xel",
    ),
    (
        "o_ucd",
        "char",
        "*",
        None,
        None,
        "meta.ucd",
        "obscore:Char.ObservableAxis.ucd",
        1,
        1,
        "p.o_ucd",
    ),
    (
        "pol_states",
        "char",
        "*",
        None,
        None,
        "meta.code;phys.polarization",
        "obscore:Char.PolarizationAxis.stateList",
        1,
        1,
        "p.pol_states",
    ),
    (
        "pol_xel",
        "long",
        None,
        None,
        None,
        "meta.number",
        "obscore:Char.PolarizationAxis.numBins",
        1,
        1,
        "p.pol_xel",
    ),
    (
        "facility_name",
        "char",
        "*",
        None,
        None,
        "meta.id;instr.tel",
        "obscore:Provenance.ObsConfig.Facility.name",
        1,
        1,
        "o.facility_name",
    ),
    (
        "instrument_name",
        "char",
        "*",
        None,
        None,
        "meta.id;instr",
        "obscore:Provenance.ObsConfig.Instrument.name",
        1,
        1,
        "o.instrument_name",
    ),
    # Non-standard companion: the pgsphere footprint derived from s_region
    # (package 7). std = 0 says it plainly; it exists so INTERSECTS/CONTAINS
    # work directly on the view.
    (
        "s_region_geom",
        "char",
        "*",
        None,
        None,
        "pos.outline;obs.field",
        None,
        0,
        0,
        "p.s_region_geom",
    ),
]

DESCRIPTIONS = {
    "dataproduct_type": "Data product (file content) primary type",
    # ObsCore 1.1 Table 6 reads this as 0=raw, 1=instrumental, 2=calibrated,
    # 3=derived; srcnet declares 0=raw, 1=calibrated, 2=science-ready,
    # 3=analysis (docs/model-schemas.md), and the view hands the value over
    # unchanged. Relabelling real calibration levels is a data-model decision,
    # not a view one, so the description states what the column holds rather
    # than what ObsCore would like it to hold; docs/obscore.md records the
    # discrepancy so a client author is not left to discover it by eye.
    "calib_level": (
        "Calibration level as declared by the SRCNet producer, passed through"
        " untranslated (srcnet: 0=raw, 1=calibrated, 2=science-ready, 3=analysis)"
    ),
    "obs_collection": "Name of the data collection",
    "obs_id": "Internal ID given by the ObsTAP service",
    "obs_publisher_did": "ID for the Dataset given by the publisher",
    "access_url": "URL used to access the dataset",
    "access_format": "Content format of the dataset",
    "access_estsize": "Estimated size of the dataset in kilobytes",
    "target_name": "Object of interest",
    "s_ra": "Central spatial position in ICRS: right ascension",
    "s_dec": "Central spatial position in ICRS: declination",
    "s_fov": "Estimated size of the covered region (diameter)",
    "s_region": "Sky region covered by the data product (STC-S)",
    "s_resolution": "Spatial resolution of the data (FWHM)",
    "s_xel1": "Number of elements along the first coordinate of the spatial axis",
    "s_xel2": "Number of elements along the second coordinate of the spatial axis",
    "t_min": "Start time in MJD",
    "t_max": "Stop time in MJD",
    "t_exptime": "Total exposure time",
    "t_resolution": "Temporal resolution (FWHM)",
    "t_xel": "Number of elements along the time axis",
    "em_min": "Start in spectral coordinates (vacuum wavelength)",
    "em_max": "Stop in spectral coordinates (vacuum wavelength)",
    "em_res_power": "Spectral resolving power",
    "em_xel": "Number of elements along the spectral axis",
    "o_ucd": "Nature of the observable axis",
    "pol_states": "List of polarization states or NULL if not applicable",
    "pol_xel": "Number of polarization samples",
    "facility_name": "Name of the facility used for this observation",
    "instrument_name": "Name of the instrument used for this observation",
    "s_region_geom": (
        "pgsphere footprint derived from s_region at ingestion (non-standard);"
        " query it with INTERSECTS/CONTAINS"
    ),
}

# doubles as the obscore table's utype in TAP_SCHEMA
DATAMODEL_IVOID = "ivo://ivoa.net/std/ObsCore#core-1.1"

# A PublisherDID lands inside a SQL string literal in the view definition,
# so its alphabet is closed: IVOA identifier characters only, quotes and
# whitespace impossible by construction.
_DID_PREFIX_RE = re.compile(r"^[A-Za-z0-9:/~?._#+-]+$")


def did_prefix() -> str:
    prefix = settings.obscore_did_prefix
    if not _DID_PREFIX_RE.match(prefix):
        raise ValueError(
            f"TAP_OBSCORE_DID_PREFIX {prefix!r} contains characters outside"
            " the IVOA identifier alphabet"
        )
    return prefix


# The DID path is built from five free-``text`` primary-key columns. RFC 3986
# unreserved is the safe core of the IVOA identifier alphabet; anything else
# in a key would either forge a path segment ('/'), truncate the identifier
# ('#', '?'), or make it unparseable (a space, a stray '%') — and a
# PublisherDID is a permanent promise, so an ambiguous one cannot be taken
# back later.
DID_SAFE_CLASS = "A-Za-z0-9._~-"

DID_KEY_COLUMNS = ("project_id", "obs_id", "sbd_id", "eb_id", "product_id")


def _did_component(column: str) -> str:
    """SQL for one percent-encoded component of the DID path.

    ``regexp_replace`` cannot compute a per-match replacement, so the
    encoding is a fold over the characters: unreserved ones survive,
    everything else becomes ``%XX`` per UTF-8 byte (upper-case hex, as RFC
    3986 recommends). ``convert_to``, ``encode`` and the ``regexp_*``
    functions are all IMMUTABLE, so this is legal in a view definition.

    The guard in front is not decoration: the fold splits the string into
    one row per character, and this view is meant to be scanned whole.
    Real identifiers are already clean, so the common row pays one anchored
    regexp match and nothing else.
    """
    return (
        f"CASE WHEN {column} ~ '^[{DID_SAFE_CLASS}]*$' THEN {column} ELSE ("
        f"SELECT string_agg(CASE WHEN ch ~ '^[{DID_SAFE_CLASS}]$' THEN ch"
        " ELSE regexp_replace(upper(encode(convert_to(ch, 'UTF8'), 'hex')),"
        " '(..)', '%\\1', 'g') END, '' ORDER BY n)"
        f" FROM regexp_split_to_table({column}, '') WITH ORDINALITY AS c(ch, n)) END"
    )


def view_sql(or_replace: bool = False) -> str:
    did = f"'{did_prefix()}' || " + " || '/' || ".join(
        _did_component(f"p.{column}") for column in DID_KEY_COLUMNS
    )
    selects = ",\n    ".join(
        f"{expression if expression is not None else did} AS {name}"
        for name, *_, expression in OBSCORE_COLUMNS
    )
    verb = "CREATE OR REPLACE VIEW" if or_replace else "CREATE VIEW"
    return (
        f"{verb} ivoa.obscore AS\n"
        f"SELECT\n    {selects}\n"
        "FROM srcnet.data_products AS p\n"
        "JOIN srcnet.observations AS o\n"
        "  ON o.project_id = p.project_id AND o.obs_id = p.obs_id\n"
        "LEFT JOIN LATERAL (\n"
        "    SELECT art.access_url, art.access_format, art.access_estsize\n"
        "    FROM srcnet.artifacts AS art\n"
        "    WHERE art.project_id = p.project_id AND art.obs_id = p.obs_id\n"
        "      AND art.sbd_id = p.sbd_id AND art.eb_id = p.eb_id\n"
        "      AND art.product_id = p.product_id AND art.semantics = 'science'\n"
        "    ORDER BY art.artifact_id\n"
        "    LIMIT 1\n"
        ") AS a ON true"
    )


# Postgres refuses CREATE OR REPLACE VIEW with invalid_table_definition when
# the replacement's column list differs in name, type or order.
_VIEW_SHAPE_CHANGED = "42P16"


def definition_comment(sql: str) -> str:
    """The view's comment, carrying a fingerprint of its own definition.

    Postgres normalises a stored view definition, so ``pg_get_viewdef``
    never compares equal to the SQL written here — a fingerprint of our own
    is the only way to recognise "nothing changed" without issuing DDL.
    """
    digest = hashlib.sha256(sql.encode()).hexdigest()[:16]
    return f"ObsCore 1.1 over the ODP metadata (definition {digest})"


def _replace_view(conn, current_comment: str | None) -> None:
    """Install ivoa.obscore, doing nothing at all when it is already current.

    Measured on PostgreSQL 16: CREATE OR REPLACE VIEW takes the same
    ACCESS EXCLUSIVE lock on the view as DROP + CREATE, so changing
    statement is not by itself the fix. That lock is held until the
    bootstrap transaction commits and the bootstrap runs on every pod start,
    so one long-running ObsCore query plus a rolling deploy is enough to
    queue every new query on ivoa.obscore behind a pod's DDL. Hence the
    fingerprint: reading a comment takes no relation lock (and GRANT takes
    none either), so a restart that is not a mapping change touches nothing.

    When the definition did change, CREATE OR REPLACE VIEW is still the
    better statement — it keeps the relation's OID, so blocked queries and
    cached plans survive it, and it keeps the grants — and it is refused
    (SQLSTATE 42P16) in exactly the case that needs the drop: a changed
    column list. A failed statement aborts the transaction, so the attempt
    runs inside a SAVEPOINT. The shape is not pre-checked against
    information_schema because the view's column types come from the select
    expressions rather than from anything declared in this module, and a
    type change missed by such a check would abort the whole bootstrap
    instead of recreating the view. The SQLSTATE is matched by code rather
    than by exception class so this module keeps reaching the database only
    through egernia_core's connection.
    """
    sql = view_sql()
    comment = definition_comment(sql)
    if current_comment == comment:
        return
    conn.execute("SAVEPOINT obscore_view")
    try:
        conn.execute(view_sql(or_replace=True))
    except Exception as exc:
        if getattr(exc, "sqlstate", None) != _VIEW_SHAPE_CHANGED:
            raise
        conn.execute("ROLLBACK TO SAVEPOINT obscore_view")
        log.info("ivoa.obscore column list changed; recreating the view (%s)", exc)
        conn.execute("DROP VIEW IF EXISTS ivoa.obscore")
        conn.execute(sql)
    else:
        conn.execute("RELEASE SAVEPOINT obscore_view")
    # COMMENT ON is DDL: Postgres plans no parameters for it, so the
    # fingerprint has to arrive already quoted rather than bound. Quoting is
    # left to the server so the escaping matches whatever the comment holds.
    literal = conn.execute("SELECT quote_literal(%s)", (comment,)).fetchone()[0]
    conn.execute(f"COMMENT ON VIEW ivoa.obscore IS {literal}")


def ensure_obscore(conn) -> None:
    """Create and register the ivoa.obscore view (odp post_ensure hook).

    Replace-in-place where possible and drop-and-create where not, so a
    mapping change migrates forward and an unchanged one is left alone (see
    :func:`_replace_view`); the caller holds the bootstrap's advisory
    transaction lock, so concurrent pods serialise here like they do on the
    rest of the schema.
    """
    existing = conn.execute(
        "SELECT c.relkind, obj_description(c.oid, 'pg_class') FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'ivoa' AND c.relname = 'obscore'"
    ).fetchone()
    if existing and existing[0] != "v":
        # A deployment that already has an ivoa.obscore *table* (its own
        # archive, or the benchmark suite's synthetic one) is publishing its
        # own ObsCore: replacing it would destroy data the service does not
        # own, and crashing on it would take the whole bootstrap down.
        log.warning(
            "ivoa.obscore already exists and is not a view; leaving it in"
            " place and skipping the ODP-derived view"
        )
        return
    conn.execute("CREATE SCHEMA IF NOT EXISTS ivoa")
    _replace_view(conn, existing[1] if existing else None)
    conn.execute(f"GRANT USAGE ON SCHEMA ivoa TO {settings.query_role}")
    conn.execute(f"GRANT SELECT ON ivoa.obscore TO {settings.query_role}")
    conn.execute(
        "INSERT INTO tap_schema.schemas (schema_name, description, schema_index)"
        " VALUES ('ivoa', 'IVOA standard tables', 50)"
        " ON CONFLICT (schema_name) DO UPDATE SET description = EXCLUDED.description"
    )
    conn.execute(
        "INSERT INTO tap_schema.tables (schema_name, table_name, table_type, utype,"
        " description, table_index)"
        " VALUES ('ivoa', 'ivoa.obscore', 'view', %s,"
        " 'ObsCore 1.1: one row per data product of the ingested ODP metadata', 1)"
        " ON CONFLICT (table_name) DO UPDATE"
        " SET utype = EXCLUDED.utype, description = EXCLUDED.description",
        (DATAMODEL_IVOID,),
    )
    for index, column in enumerate(OBSCORE_COLUMNS, start=1):
        name, datatype, arraysize, xtype, unit, ucd, utype, principal, std, _ = column
        conn.execute(
            "INSERT INTO tap_schema.columns (table_name, column_name, datatype,"
            " arraysize, xtype, unit, ucd, utype, description, indexed, principal,"
            " std, column_index)"
            " VALUES ('ivoa.obscore', %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s)"
            " ON CONFLICT (table_name, column_name) DO UPDATE SET"
            " datatype = EXCLUDED.datatype, arraysize = EXCLUDED.arraysize,"
            " xtype = EXCLUDED.xtype, unit = EXCLUDED.unit, ucd = EXCLUDED.ucd,"
            " utype = EXCLUDED.utype, description = EXCLUDED.description,"
            " principal = EXCLUDED.principal, std = EXCLUDED.std,"
            " column_index = EXCLUDED.column_index",
            (
                name,
                datatype,
                arraysize,
                xtype,
                unit,
                ucd,
                utype,
                DESCRIPTIONS[name],
                principal,
                std,
                index,
            ),
        )
