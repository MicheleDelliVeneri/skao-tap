"""VOSI endpoints: availability, capabilities (TAPRegExt), tables
(VODataService) — and the VOResource record a publishing registry harvests.

The capability elements are written once and used by both the VOSI
capabilities document and the registry record: a record that disagreed with
/capabilities about what this service supports would be worse than no record
at all.
"""

from xml.sax.saxutils import escape, quoteattr

from egernia_core.config import settings
from egernia_core.db import connection as db_connection
from egernia_core.errors import NotFoundError, ServiceError
from egernia_core.metadata.plugins import active_plugins

from ..plugins import obscore


def availability_xml() -> str:
    available = "true"
    note = "service is accepting queries"
    try:
        with db_connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover
        available = "false"
        note = f"database unreachable: {exc}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<vosi:availability xmlns:vosi="http://www.ivoa.net/xml/VOSIAvailability/v1.0">\n'
        f"  <vosi:available>{available}</vosi:available>\n"
        f"  <vosi:note>{escape(note)}</vosi:note>\n"
        "</vosi:availability>\n"
    )


def obscore_active() -> bool:
    """Whether this deployment publishes ivoa.obscore.

    Keyed off the active plugins — the same condition under which the odp
    bootstrap creates the view — so the REC's rule that the data model may
    only be declared once the table exists holds structurally.
    """
    return any(plugin.name == "odp" for plugin in active_plugins())


def _data_model_elements() -> str:
    if not obscore_active():
        return ""
    ivoid = obscore.DATAMODEL_IVOID
    return f'    <dataModel ivo-id="{ivoid}">ObsCore-1.1</dataModel>\n'


def _capability_elements() -> str:
    """The <capability> elements, indented two spaces, with no wrapper."""
    base = settings.base_url
    return f"""  <capability standardID="ivo://ivoa.net/std/TAP" xsi:type="tr:TableAccess">
    <interface xsi:type="vod:ParamHTTP" role="std" version="1.1">
      <accessURL use="base">{base}</accessURL>
    </interface>
{_data_model_elements()}    <language>
      <name>ADQL</name>
      <version ivo-id="ivo://ivoa.net/std/ADQL#v2.0">2.0</version>
      <description>ADQL 2.0 translated to PostgreSQL/pg_sphere</description>
    </language>
    <outputFormat><mime>application/x-votable+xml</mime><alias>votable</alias></outputFormat>
    <outputFormat><mime>text/csv</mime><alias>csv</alias></outputFormat>
    <outputFormat><mime>text/tab-separated-values</mime><alias>tsv</alias></outputFormat>
    <outputFormat><mime>application/json</mime><alias>json</alias></outputFormat>
    <outputFormat><mime>application/vnd.apache.parquet</mime><alias>parquet</alias></outputFormat>
    <outputFormat><mime>application/vnd.apache.arrow.stream</mime><alias>arrow</alias></outputFormat>
    <uploadMethod ivo-id="ivo://ivoa.net/std/TAPRegExt#upload-inline"/>
    <uploadMethod ivo-id="ivo://ivoa.net/std/TAPRegExt#upload-http"/>
    <uploadMethod ivo-id="ivo://ivoa.net/std/TAPRegExt#upload-https"/>
    <retentionPeriod><default>{settings.job_retention_s}</default></retentionPeriod>
    <executionDuration><default>{settings.default_exec_duration_s}</default></executionDuration>
    <outputLimit>
      <default unit="row">{settings.default_maxrec}</default>
      <hard unit="row">{settings.hard_maxrec}</hard>
    </outputLimit>
    <uploadLimit>
      <hard unit="row">{settings.upload_max_rows}</hard>
    </uploadLimit>
  </capability>
  <capability standardID="ivo://ivoa.net/std/VOSI#capabilities">
    <interface xsi:type="vod:ParamHTTP" role="std">
      <accessURL use="full">{base}/capabilities</accessURL>
    </interface>
  </capability>
  <capability standardID="ivo://ivoa.net/std/VOSI#availability">
    <interface xsi:type="vod:ParamHTTP" role="std">
      <accessURL use="full">{base}/availability</accessURL>
    </interface>
  </capability>
  <capability standardID="ivo://ivoa.net/std/VOSI#tables">
    <interface xsi:type="vod:ParamHTTP" role="std">
      <accessURL use="full">{base}/tables</accessURL>
    </interface>
  </capability>
"""


def capabilities_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<vosi:capabilities\n"
        '    xmlns:vosi="http://www.ivoa.net/xml/VOSICapabilities/v1.0"\n'
        '    xmlns:vod="http://www.ivoa.net/xml/VODataService/v1.1"\n'
        '    xmlns:tr="http://www.ivoa.net/xml/TAPRegExt/v1.0"\n'
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        f"{_capability_elements()}"
        "</vosi:capabilities>\n"
    )


SHORT_NAME_MAX = 16  # VOResource caps shortName at 16 characters


def _required(**values: str) -> dict[str, str]:
    """Every registry value that has no sensible default.

    A record missing any of these is rejected by a publishing registry, so it
    is better to say which value is missing than to serve a document that
    fails somewhere else, days later, in someone else's ingest log.
    """
    missing = sorted(name for name, value in values.items() if not value.strip())
    if missing:
        raise ServiceError(
            f"the VOResource record is enabled but incomplete; unset: {', '.join(missing)}"
        )
    return {name: value.strip() for name, value in values.items()}


def _items(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def voresource_xml() -> str:
    """The VOResource record for this service, for a publishing registry.

    Built from the deployment's own configuration and the live capability
    elements rather than kept as a file beside them, so it cannot describe a
    service other than the one answering the request.

    The child elements are deliberately in *no* namespace, and there is no
    default ``xmlns`` on the root. VOResource declares
    ``elementFormDefault="unqualified"``, so ``<title>``, ``<identifier>``,
    ``<curation>`` and ``<capability>`` are unqualified — which is what every
    published record looks like. Adding a default namespace here would
    qualify them and break schema validation.
    """
    if not settings.registry_enabled:
        raise NotFoundError(
            "this deployment publishes no VOResource record (Helm:"
            " voRegistry.enabled with an IVOA identifier; env:"
            " TAP_REGISTRY_ENABLED)"
        )
    # keyed by the Helm value an operator would go and set
    required = _required(
        **{
            "voRegistry.authorityId and voRegistry.resourceKey": settings.registry_identifier,
            "voRegistry.title": settings.registry_title,
            "voRegistry.shortName": settings.registry_short_name,
            "voRegistry.description": settings.registry_description,
            "voRegistry.referenceUrl": settings.registry_reference_url,
            "voRegistry.publisher": settings.registry_publisher,
            "voRegistry.created": settings.registry_created,
        }
    )
    identifier = required["voRegistry.authorityId and voRegistry.resourceKey"]
    if not identifier.startswith("ivo://"):
        raise ServiceError(
            "the registry identifier must start with ivo:// (Helm:"
            " voRegistry.authorityId and voRegistry.resourceKey);"
            f" got {identifier!r}"
        )
    short_name = required["voRegistry.shortName"]
    if len(short_name) > SHORT_NAME_MAX:
        raise ServiceError(
            f"voRegistry.shortName must be at most {SHORT_NAME_MAX} characters"
            f" (VOResource limit), got {len(short_name)}"
        )
    subjects = _items(settings.registry_subjects)
    if not subjects:
        raise ServiceError("the VOResource record needs at least one voRegistry.subjects entry")

    updated = settings.registry_updated.strip() or required["voRegistry.created"]
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<ri:Resource",
        '    xmlns:ri="http://www.ivoa.net/xml/RegistryInterface/v1.0"',
        # VODataService twice under two prefixes: vs: is what the xsi:type
        # reads as in every published record, and the capability elements
        # shared with /capabilities are written against vod:
        '    xmlns:vs="http://www.ivoa.net/xml/VODataService/v1.1"',
        '    xmlns:vod="http://www.ivoa.net/xml/VODataService/v1.1"',
        '    xmlns:tr="http://www.ivoa.net/xml/TAPRegExt/v1.0"',
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '    xsi:type="vs:CatalogService"',
        f"    created={quoteattr(required['voRegistry.created'])} updated={quoteattr(updated)}"
        ' status="active">',
        f"  <title>{escape(required['voRegistry.title'])}</title>",
        f"  <shortName>{escape(short_name)}</shortName>",
        f"  <identifier>{escape(identifier)}</identifier>",
        "  <curation>",
        f"    <publisher>{escape(required['voRegistry.publisher'])}</publisher>",
    ]
    if settings.registry_creator.strip():
        parts += [
            "    <creator>",
            f"      <name>{escape(settings.registry_creator.strip())}</name>",
            "    </creator>",
        ]
    contact_name = settings.registry_contact_name.strip()
    contact_email = settings.registry_contact_email.strip()
    if contact_name or contact_email:
        parts.append("    <contact>")
        if contact_name:
            parts.append(f"      <name>{escape(contact_name)}</name>")
        if contact_email:
            parts.append(f"      <email>{escape(contact_email)}</email>")
        parts.append("    </contact>")
    parts += ["  </curation>", "  <content>"]
    parts += [f"    <subject>{escape(subject)}</subject>" for subject in subjects]
    parts += [
        f"    <description>{escape(required['voRegistry.description'])}</description>",
        f"    <referenceURL>{escape(required['voRegistry.referenceUrl'])}</referenceURL>",
    ]
    parts += [f"    <type>{escape(item)}</type>" for item in _items(settings.registry_types)]
    parts += [
        f"    <contentLevel>{escape(item)}</contentLevel>"
        for item in _items(settings.registry_content_levels)
    ]
    parts += ["  </content>"]
    # the same capability elements /capabilities serves, already at the
    # indentation a child of the root element wants
    parts.append(_capability_elements().rstrip("\n"))
    parts.append("</ri:Resource>")
    return "\n".join(parts) + "\n"


def tables_xml() -> str:
    with db_connection() as conn:
        schemas = conn.execute(
            "SELECT schema_name, description FROM tap_schema.schemas ORDER BY schema_index"
        ).fetchall()
        tables = conn.execute(
            "SELECT schema_name, table_name, table_type, description, utype"
            " FROM tap_schema.tables ORDER BY table_index"
        ).fetchall()
        columns = conn.execute(
            "SELECT table_name, column_name, datatype, arraysize, description, unit, ucd,"
            " utype, xtype FROM tap_schema.columns ORDER BY column_index"
        ).fetchall()

    cols_by_table: dict[str, list] = {}
    for col in columns:
        cols_by_table.setdefault(col[0], []).append(col)
    tables_by_schema: dict[str, list] = {}
    for tab in tables:
        tables_by_schema.setdefault(tab[0], []).append(tab)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        # parenthesized so the continuation reads as one tag, not a missing comma
        (
            '<vosi:tableset xmlns:vosi="http://www.ivoa.net/xml/VOSITables/v1.0"'
            ' xmlns:vod="http://www.ivoa.net/xml/VODataService/v1.1"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        ),
    ]
    for schema_name, schema_desc in schemas:
        parts.append("  <schema>")
        parts.append(f"    <name>{escape(schema_name)}</name>")
        if schema_desc:
            parts.append(f"    <description>{escape(schema_desc)}</description>")
        for _, table_name, table_type, table_desc, table_utype in tables_by_schema.get(
            schema_name, []
        ):
            parts.append(f'    <table type="{escape(table_type or "table")}">')
            parts.append(f"      <name>{escape(table_name)}</name>")
            if table_desc:
                parts.append(f"      <description>{escape(table_desc)}</description>")
            if table_utype:
                parts.append(f"      <utype>{escape(table_utype)}</utype>")
            for (
                _,
                col_name,
                datatype,
                arraysize,
                col_desc,
                unit,
                ucd,
                utype,
                xtype,
            ) in cols_by_table.get(table_name, []):
                parts.append("      <column>")
                parts.append(f"        <name>{escape(col_name)}</name>")
                if col_desc:
                    parts.append(f"        <description>{escape(col_desc)}</description>")
                if unit:
                    parts.append(f"        <unit>{escape(unit)}</unit>")
                if ucd:
                    parts.append(f"        <ucd>{escape(ucd)}</ucd>")
                if utype:
                    parts.append(f"        <utype>{escape(utype)}</utype>")
                arr = f' arraysize="{escape(arraysize)}"' if arraysize else ""
                ext = f' extendedType="{escape(xtype)}"' if xtype else ""
                parts.append(
                    f'        <dataType xsi:type="vod:VOTableType"{arr}{ext}>'
                    f"{escape(datatype)}</dataType>"
                )
                parts.append("      </column>")
            parts.append("    </table>")
        parts.append("  </schema>")
    parts.append("</vosi:tableset>")
    return "\n".join(parts) + "\n"
