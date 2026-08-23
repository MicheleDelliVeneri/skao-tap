"""ADQL handling built on the existing `queryparser` library
(https://github.com/aipescience/queryparser, PyPI: queryparser-python3),
the ANTLR-based ADQL parser/translator used by AIP's Daiquiri framework.

ADQL geometry (POINT/CIRCLE/CONTAINS/...) is translated to pg_sphere
expressions, so the PostgreSQL backend needs the pg_sphere extension.
"""

import logging
from dataclasses import dataclass

from queryparser.adql import ADQLQueryTranslator
from queryparser.adql.ADQLParserListener import ADQLParserListener
from queryparser.exceptions import QueryError, QuerySyntaxError
from queryparser.postgresql import PostgreSQLQueryProcessor

from ..errors import QueryParseError

log = logging.getLogger("tapcore")

SUPPORTED_LANGUAGES = {"ADQL", "ADQL-2.0"}


def check_language(lang: str) -> None:
    if lang.upper() not in SUPPORTED_LANGUAGES:
        raise QueryParseError(f"LANG={lang} is not supported; supported: ADQL, ADQL-2.0")


@dataclass(frozen=True)
class Translation:
    """A translated query and the tables it reads, from one parse."""

    sql: str
    tables: frozenset[str]


class _TableCollector(ADQLParserListener):
    """Collects table names while walking a parsed ADQL query."""

    def __init__(self) -> None:
        self.tables: set[str] = set()

    # the name is ANTLR's, not ours
    def enterTable_name(self, ctx) -> None:
        self.tables.add(ctx.getText())


def translate(query: str) -> Translation:
    """Translate ADQL to PostgreSQL and list the tables, parsing once.

    The table list is what the publication check needs, and it used to come
    from a second ANTLR pass over the *translated* SQL — which cost more than
    the translation itself (locally, 39 ms against 14 ms for a point lookup).
    The translator keeps its parse tree, so the names come from a walk of the
    tree that already exists.

    ADQL-side names are also the better source: TAP_SCHEMA publishes what a
    client is allowed to write in a query, which is what this returns.
    """
    try:
        translator = ADQLQueryTranslator(query)
        sql = translator.to_postgresql()
    except QuerySyntaxError as exc:
        detail = "; ".join(str(e) for e in exc.syntax_errors) or str(exc)
        raise QueryParseError(f"ADQL syntax error: {detail}") from exc
    except QueryError as exc:
        raise QueryParseError(f"ADQL error: {exc}") from exc
    return Translation(sql=sql, tables=_tables_from_tree(translator))


def _tables_from_tree(translator: ADQLQueryTranslator) -> frozenset[str]:
    """Table names from the tree the translator already built.

    Failures are swallowed to an empty set, as the SQL-side extraction has
    always done: "no tables found" makes the caller fall back on the database
    permission layer rather than reject a query the translator accepted.
    """
    try:
        collector = _TableCollector()
        translator.walker.walk(collector, translator.tree)
        return frozenset(collector.tables)
    except Exception:  # pragma: no cover — a grammar change, not a query
        log.warning("could not read table names from the parse tree", exc_info=True)
        return frozenset()


def adql_to_postgresql(query: str) -> str:
    """Translate an ADQL query into PostgreSQL (pg_sphere) SQL."""
    return translate(query).sql


def touched_tables(sql: str) -> set[str]:
    """Best-effort extraction of the tables referenced by *translated* SQL.

    Prefer :func:`translate`, which gets the same answer from the ADQL parse
    it already did. This one exists for the executor, which reads the SQL
    stored on a job and has no ADQL tree to walk — it pays a full parse, once
    per job rather than once per request.
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
