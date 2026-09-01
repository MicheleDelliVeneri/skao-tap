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
derives from ``s_region``, registered with ``std = 0`` so ADQL
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
from typing import NamedTuple

from egernia_core.config import settings

log = logging.getLogger("tap-api")


class ObsCoreColumn(NamedTuple):
    """One ivoa.obscore column: its TAP_SCHEMA registration and the view
    expression that produces it, in REC Table 6 order.

    arraysize is derived rather than stored — VOTable wants "*" for char
    and nothing for the fixed-width types, which held for all 31 columns when
    they were spelled out. Everything ObsCore makes near-universal
    (``principal``, ``std``) defaults to the standard value, so only the one
    non-standard column has to say otherwise.
    """

    name: str
    datatype: str  # the VOTable name TAP_SCHEMA uses
    ucd: str
    utype: str | None
    description: str
    expression: str | None  # None: the publisher DID, built by view_sql()
    unit: str | None = None
    xtype: str | None = None
    principal: int = 1
    std: int = 1

    @property
    def arraysize(self) -> str | None:
        return "*" if self.datatype == "char" else None


OBSCORE_COLUMNS: list[ObsCoreColumn] = [
    ObsCoreColumn(
        "dataproduct_type",
        "char",
        # meta.code.class, not meta.id: changed by ObsCore 1.1 Erratum 1
        "meta.code.class",
        "obscore:ObsDataset.dataProductType",
        "Data product (file content) primary type",
        "CASE p.dataproduct_type WHEN 'table' THEN 'measurements' ELSE p.dataproduct_type END",
    ),
    # ObsCore 1.1 Table 6 reads calib_level as 0=raw, 1=instrumental,
    # 2=calibrated, 3=derived; srcnet declares 0=raw, 1=calibrated,
    # 2=science-ready, 3=analysis (docs/model-schemas.md), and the view hands
    # the value over unchanged. Relabelling real calibration levels is a
    # data-model decision, not a view one, so the description states what the
    # column holds rather than what ObsCore would like it to hold;
    # docs/obscore.md records the discrepancy.
    ObsCoreColumn(
        "calib_level",
        "int",
        "meta.code;obs.calib",
        "obscore:ObsDataset.calibLevel",
        "Calibration level as declared by the SRCNet producer, passed through"
        " untranslated (srcnet: 0=raw, 1=calibrated, 2=science-ready, 3=analysis)",
        "p.calib_level::integer",
    ),
    ObsCoreColumn(
        "obs_collection",
        "char",
        "meta.id",
        "obscore:DataID.collection",
        "Name of the data collection",
        "COALESCE(o.collection, 'unclassified')",
    ),
    ObsCoreColumn(
        "obs_id",
        "char",
        "meta.id",
        "obscore:DataID.observationID",
        "Internal ID given by the ObsTAP service",
        "p.obs_id",
    ),
    ObsCoreColumn(
        "obs_publisher_did",
        "char",
        # meta.ref.ivoid, not meta.ref.uri;meta.curation: ObsCore 1.1 Erratum
        "meta.ref.ivoid",
        "obscore:Curation.publisherDID",
        "ID for the Dataset given by the publisher",
        None,
    ),
    ObsCoreColumn(
        "access_url",
        "char",
        "meta.ref.url",
        "obscore:Access.reference",
        "URL used to access the dataset",
        "a.access_url",
    ),
    ObsCoreColumn(
        "access_format",
        "char",
        "meta.code.mime",
        "obscore:Access.format",
        "Content format of the dataset",
        "a.access_format",
    ),
    ObsCoreColumn(
        "access_estsize",
        "long",
        "phys.size;meta.file",
        "obscore:Access.size",
        "Estimated size of the dataset in kilobytes",
        "round(a.access_estsize / 1000.0)::bigint",
        unit="kbyte",
    ),
    ObsCoreColumn(
        "target_name",
        "char",
        "meta.id;src",
        "obscore:Target.name",
        "Object of interest",
        "p.target_name",
    ),
    ObsCoreColumn(
        "s_ra",
        "double",
        "pos.eq.ra",
        "obscore:Char.SpatialAxis.Coverage.Location.Coord.Position2D.Value2.C1",
        "Central spatial position in ICRS: right ascension",
        "p.s_ra",
        unit="deg",
    ),
    ObsCoreColumn(
        "s_dec",
        "double",
        "pos.eq.dec",
        "obscore:Char.SpatialAxis.Coverage.Location.Coord.Position2D.Value2.C2",
        "Central spatial position in ICRS: declination",
        "p.s_dec",
        unit="deg",
    ),
    ObsCoreColumn(
        "s_fov",
        "double",
        "phys.angSize;instr.fov",
        "obscore:Char.SpatialAxis.Coverage.Bounds.Extent.diameter",
        "Estimated size of the covered region (diameter)",
        "p.s_fov",
        unit="deg",
    ),
    ObsCoreColumn(
        "s_region",
        "char",
        "pos.outline;obs.field",
        "obscore:Char.SpatialAxis.Coverage.Support.Area",
        "Sky region covered by the data product (STC-S)",
        "p.s_region",
        xtype="adql:REGION",
    ),
    ObsCoreColumn(
        "s_resolution",
        "double",
        "pos.angResolution",
        "obscore:Char.SpatialAxis.Resolution.Refval.value",
        "Spatial resolution of the data (FWHM)",
        "p.beam_size",
        unit="arcsec",
    ),
    ObsCoreColumn(
        "s_xel1",
        "long",
        "meta.number",
        "obscore:Char.SpatialAxis.numBins1",
        "Number of elements along the first coordinate of the spatial axis",
        "p.s_xel1",
    ),
    ObsCoreColumn(
        "s_xel2",
        "long",
        "meta.number",
        "obscore:Char.SpatialAxis.numBins2",
        "Number of elements along the second coordinate of the spatial axis",
        "p.s_xel2",
    ),
    ObsCoreColumn(
        "t_min",
        "double",
        "time.start;obs.exposure",
        "obscore:Char.TimeAxis.Coverage.Bounds.Limits.StartTime",
        "Start time in MJD",
        "p.t_min",
        unit="d",
    ),
    ObsCoreColumn(
        "t_max",
        "double",
        "time.end;obs.exposure",
        "obscore:Char.TimeAxis.Coverage.Bounds.Limits.StopTime",
        "Stop time in MJD",
        "p.t_max",
        unit="d",
    ),
    ObsCoreColumn(
        "t_exptime",
        "double",
        "time.duration;obs.exposure",
        "obscore:Char.TimeAxis.Coverage.Support.Extent",
        "Total exposure time",
        "p.t_exptime",
        unit="s",
    ),
    ObsCoreColumn(
        "t_resolution",
        "double",
        "time.resolution",
        "obscore:Char.TimeAxis.Resolution.Refval.value",
        "Temporal resolution (FWHM)",
        "NULL::double precision",
        unit="s",
    ),
    ObsCoreColumn(
        "t_xel",
        "long",
        "meta.number",
        "obscore:Char.TimeAxis.numBins",
        "Number of elements along the time axis",
        "p.t_xel",
    ),
    ObsCoreColumn(
        "em_min",
        "double",
        "em.wl;stat.min",
        "obscore:Char.SpectralAxis.Coverage.Bounds.Limits.LoLimit",
        "Start in spectral coordinates (vacuum wavelength)",
        "p.em_min",
        unit="m",
    ),
    ObsCoreColumn(
        "em_max",
        "double",
        "em.wl;stat.max",
        "obscore:Char.SpectralAxis.Coverage.Bounds.Limits.HiLimit",
        "Stop in spectral coordinates (vacuum wavelength)",
        "p.em_max",
        unit="m",
    ),
    ObsCoreColumn(
        "em_res_power",
        "double",
        "spect.resolution",
        "obscore:Char.SpectralAxis.Resolution.ResolPower.refVal",
        "Spectral resolving power",
        "NULL::double precision",
    ),
    ObsCoreColumn(
        "em_xel",
        "long",
        "meta.number",
        "obscore:Char.SpectralAxis.numBins",
        "Number of elements along the spectral axis",
        "p.em_xel",
    ),
    ObsCoreColumn(
        "o_ucd",
        "char",
        "meta.ucd",
        "obscore:Char.ObservableAxis.ucd",
        "Nature of the observable axis",
        "p.o_ucd",
    ),
    ObsCoreColumn(
        "pol_states",
        "char",
        "meta.code;phys.polarization",
        "obscore:Char.PolarizationAxis.stateList",
        "List of polarization states or NULL if not applicable",
        "p.pol_states",
    ),
    ObsCoreColumn(
        "pol_xel",
        "long",
        "meta.number",
        "obscore:Char.PolarizationAxis.numBins",
        "Number of polarization samples",
        "p.pol_xel",
    ),
    ObsCoreColumn(
        "facility_name",
        "char",
        "meta.id;instr.tel",
        "obscore:Provenance.ObsConfig.Facility.name",
        "Name of the facility used for this observation",
        "o.facility_name",
    ),
    ObsCoreColumn(
        "instrument_name",
        "char",
        "meta.id;instr",
        "obscore:Provenance.ObsConfig.Instrument.name",
        "Name of the instrument used for this observation",
        "o.instrument_name",
    ),
    # Non-standard companion: the pgsphere footprint derived from s_region.
    # std = 0 says it plainly; it exists so INTERSECTS/CONTAINS work directly
    # on the view.
    ObsCoreColumn(
        "s_region_geom",
        "char",
        "pos.outline;obs.field",
        None,
        "pgsphere footprint derived from s_region at ingestion (non-standard);"
        " query it with INTERSECTS/CONTAINS",
        "p.s_region_geom",
        principal=0,
        std=0,
    ),
]

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

# A configured column name is interpolated into the view's DDL, where no
# parameter can be bound, so the alphabet is closed to plain lower-case SQL
# identifiers. Anything else — a quote, a space, a parenthesis — is refused
# before it reaches a CREATE VIEW.
_DID_COLUMN_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def did_key_columns() -> tuple[str, ...]:
    """The data_products columns whose values form the DID path, in order.

    Configured (``TAP_OBSCORE_DID_COLUMNS``, dot-separated) because a
    deployment whose ODP model nests differently has a different identity
    chain. Two things are the operator's to get right and neither can be
    checked here: the chain has to identify a data product *uniquely*, or two
    products share a DID; and changing it changes every DID this service has
    ever published, which a permanent identifier is not supposed to do.

    A column that does not exist on srcnet.data_products fails at bootstrap,
    where PostgreSQL names it.
    """
    raw = settings.obscore_did_columns
    columns = tuple(part.strip() for part in raw.split(".") if part.strip())
    if not columns:
        raise ValueError("TAP_OBSCORE_DID_COLUMNS is empty; a DID needs at least one column")
    for column in columns:
        if not _DID_COLUMN_RE.match(column):
            raise ValueError(
                f"TAP_OBSCORE_DID_COLUMNS component {column!r} is not a plain SQL"
                " identifier (lower-case letters, digits and underscores)"
            )
    return columns


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
        _did_component(f"p.{column}") for column in did_key_columns()
    )
    selects = ",\n    ".join(
        f"{column.expression if column.expression is not None else did} AS {column.name}"
        for column in OBSCORE_COLUMNS
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
        # archive, or a test harness's synthetic one) is publishing its
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
                column.name,
                column.datatype,
                column.arraysize,
                column.xtype,
                column.unit,
                column.ucd,
                column.utype,
                column.description,
                column.principal,
                column.std,
                index,
            ),
        )
