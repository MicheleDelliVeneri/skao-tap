"""ADQL handling built on the existing `queryparser` library
(https://github.com/aipescience/queryparser, PyPI: queryparser-python3),
the ANTLR-based ADQL parser/translator used by AIP's Daiquiri framework.

ADQL geometry (POINT/CIRCLE/CONTAINS/...) is translated to pg_sphere
expressions, so the PostgreSQL backend needs the pg_sphere extension.
"""

import logging
import re
from dataclasses import dataclass

import antlr4
from antlr4 import BailErrorStrategy, PredictionMode
from queryparser.adql import ADQLQueryTranslator
from queryparser.adql.ADQLLexer import ADQLLexer
from queryparser.adql.ADQLParser import ADQLParser
from queryparser.adql.ADQLParserListener import ADQLParserListener
from queryparser.adql.adqltranslator import SyntaxErrorListener
from queryparser.exceptions import QueryError, QuerySyntaxError
from queryparser.postgresql import PostgreSQLQueryProcessor

from ..errors import QueryParseError
from ..observability import ADQL_SLOW_PARSES

log = logging.getLogger("egernia_core")

SUPPORTED_LANGUAGES = {"ADQL", "ADQL-2.0"}


def check_language(lang: str) -> None:
    if lang.upper() not in SUPPORTED_LANGUAGES:
        raise QueryParseError(f"LANG={lang} is not supported; supported: ADQL, ADQL-2.0")


@dataclass(frozen=True)
class Translation:
    """A translated query and the tables it reads, from one parse."""

    sql: str
    tables: frozenset[str]


class _Translator(ADQLQueryTranslator):
    """The library's translator with the two ways it wastes time removed.

    Both are in the parse, and the parse is where essentially all of a
    request's CPU goes: measured on this corpus, translation is 41 ms of a
    ~50 ms request, so the service's single-core ceiling is set here and
    nowhere else.

    **One parse instead of two.** The base class parses in ``set_query`` (from
    the constructor) and then ``to_postgresql`` parses again, throwing the
    first tree away. Storing the query without parsing leaves exactly the one
    parse that produces the tree actually used.

    **SLL prediction first.** ANTLR's default full-context (LL) prediction was
    71% of the profile — 42,000 closure operations per query. The standard
    two-stage strategy is to try the cheap SLL mode with an error strategy that
    bails out immediately, and re-parse with the library's own full-context
    path if anything at all goes wrong. A query SLL cannot handle therefore
    still gets exactly the parse it would have got before; the fast path is
    only ever taken when it succeeds outright.
    """

    def set_query(self, query):
        # Deliberately does not parse: to_postgresql() does, and the base
        # class's eager parse here is discarded a moment later.
        self._query = query.lstrip("\n").rstrip().rstrip(";") + ";"

    def parse(self):
        try:
            self._parse_sll()
        except Exception as exc:
            # Includes ParseCancellationException from the bail strategy, and
            # anything else the fast path trips over. The slow path is the
            # library's own, so behaviour for such a query is unchanged —
            # including which syntax errors it reports.
            #
            # Logged on the way in and counted only once the slow parse
            # succeeds. An invalid query also reaches here — SLL bails on it —
            # and counting that would put user errors into a metric whose
            # stated meaning is "the fast path stopped working", so a burst of
            # bad ADQL would read as a performance regression. Invalid queries
            # are already visible as 4xx responses.
            log.debug("ADQL fast parse fell back to full context: %s", exc)
            super().parse()
            ADQL_SLOW_PARSES.inc()

    def _parse_sll(self):
        stream = antlr4.CommonTokenStream(ADQLLexer(antlr4.InputStream(self.query)))
        parser = ADQLParser(stream)
        parser._interp.predictionMode = PredictionMode.SLL
        parser._errHandler = BailErrorStrategy()
        listener = SyntaxErrorListener()
        # The public listener API rather than assigning _listeners: the default
        # console listener has to go (a parse attempt is not a user-visible
        # error yet), and reaching into the runtime's internals to do it would
        # be one antlr4 release away from breaking.
        parser.removeErrorListeners()
        parser.addErrorListener(listener)
        tree = parser.query()
        if listener.syntax_errors:
            # Not raised here: a syntax error under SLL may be an artefact of
            # SLL rather than a real one, so the slow path decides.
            raise QuerySyntaxError(listener.syntax_errors)
        self.stream = stream
        self.parser = parser
        self.syntax_error_listener = listener
        self.tree = tree
        self.walker = antlr4.ParseTreeWalker()


class _TableCollector(ADQLParserListener):
    """Collects table names while walking a parsed ADQL query."""

    def __init__(self) -> None:
        self.tables: set[str] = set()

    # the name is ANTLR's, not ours
    def enterTable_name(self, ctx) -> None:
        self.tables.add(ctx.getText())


# ---------------------------------------------------------------------------
# Geometry-typed columns as INTERSECTS/CONTAINS arguments
# ---------------------------------------------------------------------------
#
# ADQL 2.1 allows a column of geometry type where INTERSECTS and CONTAINS
# take a region — which is how the metadata domains' derived footprint
# columns (s_region_geom, pgsphere spoly) are queried. The queryparser
# grammar predates that: it accepts only geometry *constructors* there, and
# a bare column reference is a syntax error. Rather than fork the grammar,
# the column is hidden from the parser behind a sentinel POLYGON literal
# whose coordinates are unique magic numbers, and the pgsphere literal the
# translator deterministically emits for that sentinel is swapped back for
# the column name in the SQL. The sentinel's shape is pinned by unit tests,
# so a queryparser upgrade that changes its output fails loudly here rather
# than quietly producing wrong SQL.

_GEOMETRY_PREDICATES = ("INTERSECTS", "CONTAINS")
_SENTINEL_RA = 654321.0  # far outside [0, 360]; no real query writes this
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


def _sentinel_adql(index: int) -> str:
    ra = _SENTINEL_RA + index
    return f"POLYGON('ICRS', {ra}, 1.0, {ra}, 2.0, {ra}, 3.0)"


def _sentinel_sql(index: int) -> str:
    ra = _SENTINEL_RA + index
    return f"spoly('{{({ra}d,1.0d),({ra}d,2.0d),({ra}d,3.0d)}}')"


def _predicate_spans(query: str):
    """(args_start, args_end) index pairs of every INTERSECTS/CONTAINS
    argument list, ignoring string literals."""
    upper = query.upper()
    spans = []
    i = 0
    while i < len(query):
        if query[i] == "'":  # skip string literals ('' is an escaped quote)
            i += 1
            while i < len(query):
                if query[i] == "'":
                    i += 1
                    if i < len(query) and query[i] == "'":
                        i += 1
                        continue
                    break
                i += 1
            continue
        matched = next(
            (
                p
                for p in _GEOMETRY_PREDICATES
                if upper.startswith(p, i)
                and (i == 0 or not (query[i - 1].isalnum() or query[i - 1] == "_"))
            ),
            None,
        )
        if matched is None:
            i += 1
            continue
        j = i + len(matched)
        while j < len(query) and query[j].isspace():
            j += 1
        if j >= len(query) or query[j] != "(":
            i += 1
            continue
        depth, k = 0, j
        while k < len(query):
            if query[k] == "'":
                k += 1
                while k < len(query) and query[k] != "'":
                    k += 1
            elif query[k] == "(":
                depth += 1
            elif query[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if depth == 0 and k < len(query):
            spans.append((j + 1, k))
            i = j + 1  # nested predicates inside the args are found too
        else:
            i += 1
    return spans


def _hide_geometry_columns(query: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace bare-column geometry arguments with sentinel literals.

    Returns the rewritten query and [(sql_literal, column_text), ...] for
    the restore pass. A query with no such arguments comes back unchanged.
    """
    replacements: list[tuple[int, int, str]] = []  # (start, end, new_text)
    found: list[tuple[str, str]] = []
    for start, end in _predicate_spans(query):
        arg_start, depth = start, 0
        pieces = []
        k = start
        while k <= end:
            if k == end or (query[k] == "," and depth == 0):
                pieces.append((arg_start, k))
                arg_start = k + 1
            elif query[k] == "(":
                depth += 1
            elif query[k] == ")":
                depth -= 1
            elif query[k] == "'":
                k += 1
                while k <= end and query[k] != "'":
                    k += 1
            k += 1
        for a, b in pieces:
            text = query[a:b].strip()
            if _IDENTIFIER.match(text):
                index = len(found)
                found.append((_sentinel_sql(index), text))
                replacements.append((a, b, _sentinel_adql(index)))
    if not found:
        return query, []
    rewritten = []
    last = 0
    for a, b, new_text in sorted(replacements):
        rewritten.append(query[last:a])
        rewritten.append(new_text)
        last = b
    rewritten.append(query[last:])
    return "".join(rewritten), found


def _restore_geometry_columns(sql: str, found: list[tuple[str, str]]) -> str:
    for literal, column in found:
        if sql.count(literal) != 1:
            # a queryparser upgrade changed how the sentinel renders: this
            # is a service defect, not a user error, so fail loudly
            raise RuntimeError(
                "geometry-column substitution failed: sentinel literal not"
                f" found exactly once in the translated SQL ({literal!r})"
            )
        sql = sql.replace(literal, column)
    return sql


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
    prepared, geometry_columns = _hide_geometry_columns(query)
    try:
        translator = _Translator(prepared)
        sql = translator.to_postgresql()
    except QuerySyntaxError as exc:
        detail = "; ".join(str(e) for e in exc.syntax_errors) or str(exc)
        raise QueryParseError(f"ADQL syntax error: {detail}") from exc
    except QueryError as exc:
        raise QueryParseError(f"ADQL error: {exc}") from exc
    sql = _restore_geometry_columns(sql, geometry_columns)
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
