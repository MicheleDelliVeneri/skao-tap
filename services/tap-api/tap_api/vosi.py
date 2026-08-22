"""VOSI endpoints: availability, capabilities (TAPRegExt), tables (VODataService)."""

from xml.sax.saxutils import escape

from tapcore.config import settings
from tapcore.db import pool


def availability_xml() -> str:
    available = "true"
    note = "service is accepting queries"
    try:
        with pool().connection() as conn:
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


def capabilities_xml() -> str:
    base = settings.base_url
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<vosi:capabilities
    xmlns:vosi="http://www.ivoa.net/xml/VOSICapabilities/v1.0"
    xmlns:vod="http://www.ivoa.net/xml/VODataService/v1.1"
    xmlns:tr="http://www.ivoa.net/xml/TAPRegExt/v1.0"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <capability standardID="ivo://ivoa.net/std/TAP" xsi:type="tr:TableAccess">
    <interface xsi:type="vod:ParamHTTP" role="std" version="1.1">
      <accessURL use="base">{base}</accessURL>
    </interface>
    <language>
      <name>ADQL</name>
      <version ivo-id="ivo://ivoa.net/std/ADQL#v2.0">2.0</version>
      <description>ADQL 2.0 translated to PostgreSQL/pg_sphere</description>
    </language>
    <outputFormat><mime>application/x-votable+xml</mime><alias>votable</alias></outputFormat>
    <outputFormat><mime>text/csv</mime><alias>csv</alias></outputFormat>
    <outputFormat><mime>text/tab-separated-values</mime><alias>tsv</alias></outputFormat>
    <outputFormat><mime>application/json</mime><alias>json</alias></outputFormat>
    <retentionPeriod><default>{settings.job_retention_s}</default></retentionPeriod>
    <executionDuration><default>{settings.default_exec_duration_s}</default></executionDuration>
    <outputLimit>
      <default unit="row">{settings.default_maxrec}</default>
      <hard unit="row">{settings.hard_maxrec}</hard>
    </outputLimit>
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
</vosi:capabilities>
"""


def tables_xml() -> str:
    with pool().connection() as conn:
        schemas = conn.execute(
            "SELECT schema_name, description FROM tap_schema.schemas ORDER BY schema_index"
        ).fetchall()
        tables = conn.execute(
            "SELECT schema_name, table_name, table_type, description"
            " FROM tap_schema.tables ORDER BY table_index"
        ).fetchall()
        columns = conn.execute(
            "SELECT table_name, column_name, datatype, arraysize, description, unit, ucd"
            " FROM tap_schema.columns ORDER BY column_index"
        ).fetchall()

    cols_by_table: dict[str, list] = {}
    for col in columns:
        cols_by_table.setdefault(col[0], []).append(col)
    tables_by_schema: dict[str, list] = {}
    for tab in tables:
        tables_by_schema.setdefault(tab[0], []).append(tab)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<vosi:tableset xmlns:vosi="http://www.ivoa.net/xml/VOSITables/v1.0"'
        ' xmlns:vod="http://www.ivoa.net/xml/VODataService/v1.1"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
    ]
    for schema_name, schema_desc in schemas:
        parts.append("  <schema>")
        parts.append(f"    <name>{escape(schema_name)}</name>")
        if schema_desc:
            parts.append(f"    <description>{escape(schema_desc)}</description>")
        for _, table_name, table_type, table_desc in tables_by_schema.get(schema_name, []):
            parts.append(f'    <table type="{escape(table_type or "table")}">')
            parts.append(f"      <name>{escape(table_name)}</name>")
            if table_desc:
                parts.append(f"      <description>{escape(table_desc)}</description>")
            for _, col_name, datatype, arraysize, col_desc, unit, ucd in cols_by_table.get(
                table_name, []
            ):
                parts.append("      <column>")
                parts.append(f"        <name>{escape(col_name)}</name>")
                if col_desc:
                    parts.append(f"        <description>{escape(col_desc)}</description>")
                if unit:
                    parts.append(f"        <unit>{escape(unit)}</unit>")
                if ucd:
                    parts.append(f"        <ucd>{escape(ucd)}</ucd>")
                arr = f' arraysize="{escape(arraysize)}"' if arraysize else ""
                parts.append(
                    f'        <dataType xsi:type="vod:VOTableType"{arr}>'
                    f"{escape(datatype)}</dataType>"
                )
                parts.append("      </column>")
            parts.append("    </table>")
        parts.append("  </schema>")
    parts.append("</vosi:tableset>")
    return "\n".join(parts) + "\n"
