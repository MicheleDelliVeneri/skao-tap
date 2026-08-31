"""ADQL handling built on this package's vendored fork of `queryparser`
(https://github.com/aipescience/queryparser, PyPI: queryparser-python3),
the ANTLR-based ADQL parser/translator used by AIP's Daiquiri framework.
The fork (see `_adql/README.md`) extends the grammar to ADQL 2.1; the
non-forked parts of queryparser (PostgreSQL processor, exceptions) are
still used from the PyPI package.

ADQL geometry (POINT/CIRCLE/CONTAINS/...) is translated to pg_sphere
expressions, so the PostgreSQL backend needs the pg_sphere extension.
"""

import logging
import threading
from dataclasses import dataclass
from functools import lru_cache

import antlr4
from antlr4 import BailErrorStrategy, PredictionMode
from queryparser.exceptions import QueryError, QuerySyntaxError
from queryparser.postgresql import PostgreSQLQueryProcessor

from ..config import settings
from ..errors import QueryParseError
from ..observability import (
    ADQL_SLOW_PARSES,
    ADQL_TRANSLATION_HITS,
    ADQL_TRANSLATION_MISSES,
)
from ._adql import (
    ADQLLexer,
    ADQLParser,
    ADQLParserListener,
    ADQLQueryTranslator,
    SyntaxErrorListener,
)

log = logging.getLogger("egernia_core")

SUPPORTED_LANGUAGES = {"ADQL", "ADQL-2.0", "ADQL-2.1"}


def check_language(lang: str) -> None:
    if lang.upper() not in SUPPORTED_LANGUAGES:
        raise QueryParseError(f"LANG={lang} is not supported; supported: ADQL, ADQL-2.0, ADQL-2.1")


@dataclass(frozen=True)
class Translation:
    """A translated query and the tables it reads, from one parse.

    ``geometry_columns`` holds the column references (as written in the
    query, possibly qualified) that the translator accepted in geometry
    slots — CONTAINS/INTERSECTS/AREA arguments, DISTANCE point arguments,
    CIRCLE centres. Translation is pure and consults no TAP_SCHEMA, so a
    text column such as ObsCore's ``s_region`` passes through here and
    would only fail inside PostgreSQL; a caller that has column metadata
    uses this list to refuse it with a usage error instead.
    """

    sql: str
    tables: frozenset[str]
    geometry_columns: frozenset[str]


class _Translator(ADQLQueryTranslator):
    """The library's translator with the two ways it wastes time removed.

    Both are in the parse, and the parse is where essentially all of a
    request's CPU goes, so the service's single-core ceiling is set here and
    nowhere else (measurements: docs/python-performance.md).

    **One parse instead of two.** The base class parses in ``set_query`` (from
    the constructor) and then ``to_postgresql`` parses again, throwing the
    first tree away. Storing the query without parsing leaves exactly the one
    parse that produces the tree actually used.

    **SLL prediction first.** ANTLR's default full-context (LL) prediction
    dominates the parse. The standard two-stage strategy is to try the cheap
    SLL mode with an error strategy that bails out immediately, and re-parse
    with the library's own full-context path if anything at all goes wrong.
    A query SLL cannot handle therefore still gets exactly the parse it
    would have got before; the fast path is only ever taken when it
    succeeds outright.
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


def _normalise(query: str) -> str:
    """The cache key: the query with what cannot change its translation cut.

    Only leading whitespace and a trailing semicolon (with the whitespace
    around it) — verified to translate identically, and neither can occur at
    the end of a string literal, so neither can be part of one.

    Deliberately *not* case-folded and *not* whitespace-collapsed.
    Translation is not case-insensitive where it matters: ``name = 'AbC'``
    and ``name = 'abc'`` translate to different SQL,
    so folding the key would serve one query the other's results. Collapsing
    runs of whitespace has the same flaw inside a literal. Turning more
    near-misses into hits means a parameterising translator, which is a much
    larger change than this one.
    """
    return query.lstrip().rstrip("; \t\n\r\f\v")


#: Set by :func:`translate` before the call and cleared by the body below, so
#: the caller learns whether it got a hit without reading a shared counter.
#: `lru_cache` runs the wrapped body on the *calling* thread, so a thread-local
#: is exact where a before/after read of `cache_info().misses` is not: the
#: request path is `run_in_threadpool(prepare_query, ...)` at five call sites,
#: and a miss holds its window open for the whole parse, during which every
#: concurrent hit would read the miss as its own. This version cannot
#: misreport, and is cheaper -- one attribute write against two
#: `cache_info()` calls.
_outcome = threading.local()


@lru_cache(maxsize=settings.translation_cache_size)
def _translated(query: str) -> Translation:
    """The memoised translation. Call :func:`translate`, not this.

    The body runs only on a miss, which is what makes the flag exact.
    """
    _outcome.hit = False
    try:
        translator = _Translator(query)
        sql = translator.to_postgresql()
    except QuerySyntaxError as exc:
        detail = "; ".join(str(e) for e in exc.syntax_errors) or str(exc)
        raise QueryParseError(f"ADQL syntax error: {detail}") from exc
    except QueryError as exc:
        raise QueryParseError(f"ADQL error: {exc}") from exc
    return Translation(
        sql=sql,
        tables=_tables_from_tree(translator),
        geometry_columns=translator.geometry_columns,
    )


def translate(query: str) -> Translation:
    """Translate ADQL to PostgreSQL and list the tables, parsing once.

    The table list is what the publication check needs. The translator keeps
    its parse tree, so the names come from a walk of the tree that already
    exists — never from a second parse.

    ADQL-side names are also the better source: TAP_SCHEMA publishes what a
    client is allowed to write in a query, which is what this returns.

    **The result is memoised, and that rests on translation being pure.** This
    is a function of the query text alone: it consults no TAP_SCHEMA, no
    connection and no principal, and returns a frozen ``Translation``, so a
    hit is indistinguishable from a miss. Anything that makes it depend on
    something else — a column lookup, per-user behaviour, a mutable field on
    ``Translation`` — breaks the cache silently, serving one caller another's
    answer. Add it in the caller, not here. ``tests/unit/test_adql.py`` pins
    the property.

    Failures are never cached: ``lru_cache`` stores nothing when the call
    raises, so a ``QueryParseError`` re-parses every time and a client cannot
    fill the cache with its own mistakes.
    """
    _outcome.hit = True
    result = _translated(_normalise(query))
    (ADQL_TRANSLATION_HITS if _outcome.hit else ADQL_TRANSLATION_MISSES).inc()
    return result


# So callers, tests and the microbenchmarks reach the cache through the public
# name rather than the private one.
translate.cache_clear = _translated.cache_clear
translate.cache_info = _translated.cache_info


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
    it already did. This is a fallback for the executor, for jobs queued by
    an API that predates the ``query_tables`` column — and it is expensive:
    a full ANTLR parse of the SQL, 100-190 ms per query on this corpus
    (``PostgreSQLQueryProcessor`` has none of the SLL fast path the
    translator's parse got), where the translation itself is 1-2 ms.
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
