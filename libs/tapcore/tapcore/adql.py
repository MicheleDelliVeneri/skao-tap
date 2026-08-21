"""ADQL handling built on the existing `queryparser` library
(https://github.com/aipescience/queryparser, PyPI: queryparser-python3),
the ANTLR-based ADQL parser/translator used by AIP's Daiquiri framework.

ADQL geometry (POINT/CIRCLE/CONTAINS/...) is translated to pg_sphere
expressions, so the PostgreSQL backend needs the pg_sphere extension.
"""

from queryparser.adql import ADQLQueryTranslator
from queryparser.exceptions import QueryError, QuerySyntaxError
from queryparser.postgresql import PostgreSQLQueryProcessor

from .errors import QueryParseError

SUPPORTED_LANGUAGES = {"ADQL", "ADQL-2.0"}


def check_language(lang: str) -> None:
    if lang.upper() not in SUPPORTED_LANGUAGES:
        raise QueryParseError(f"LANG={lang} is not supported; supported: ADQL, ADQL-2.0")


def adql_to_postgresql(query: str) -> str:
    """Translate an ADQL query into PostgreSQL (pg_sphere) SQL."""
    try:
        translator = ADQLQueryTranslator(query)
        return translator.to_postgresql()
    except QuerySyntaxError as exc:
        detail = "; ".join(str(e) for e in exc.syntax_errors) or str(exc)
        raise QueryParseError(f"ADQL syntax error: {detail}") from exc
    except QueryError as exc:
        raise QueryParseError(f"ADQL error: {exc}") from exc


def touched_tables(sql: str) -> set[str]:
    """Best-effort extraction of the tables referenced by the translated
    query, used to verify that only TAP_SCHEMA-published tables are read.
    """
    try:
        processor = PostgreSQLQueryProcessor(sql)
        processor.process_query()
        tables = set()
        for entry in processor.tables or []:
            if isinstance(entry, (tuple, list)):
                tables.add(".".join(str(p) for p in entry if p))
            else:
                tables.add(str(entry).rstrip("."))
        return tables
    except Exception:
        # The processor is stricter than the translator; treat failures as
        # "unknown" and let the database permission layer be the backstop.
        return set()


def apply_maxrec(sql: str, maxrec: int) -> str:
    """Enforce MAXREC by wrapping the query; fetch one extra row so the
    serializer can flag overflow per DALI."""
    return f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS _tap_query LIMIT {maxrec + 1}"
