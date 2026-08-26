# -*- coding: utf-8 -*-
"""ADQL-to-SQL translator, forked from queryparser 0.7.4.

Vendored from https://github.com/aipescience/queryparser (Apache-2.0,
``src/queryparser/adql/adqltranslator.py``) together with the grammar it
walks, regenerated from the forked ``.g4`` files in this directory. The fork
exists because upstream's grammar predates ADQL 2.1 and is unmaintained; see
README.md here for the grammar diff and regeneration instructions.

Changes against upstream, all ADQL 2.1:

- geometry arguments (CONTAINS/INTERSECTS/AREA/DISTANCE, circle centres) may
  be column references, not just constructors — the column name passes
  through to the SQL, where it is a pgsphere-typed column; every column
  reference accepted this way is recorded on the translator as
  ``geometry_columns`` (the translator itself has no column metadata, so a
  caller with TAP_SCHEMA access type-checks them — a *text* column here
  would otherwise only fail inside PostgreSQL);
- CAST and COALESCE render as themselves (both are PostgreSQL-native); CAST
  to the geometry types the grammar admits is rejected with a clear error
  because no pgsphere mapping is defined for it;
- LOWER/UPPER accept any character expression, not just string literals;
- EXCEPT and INTERSECT are no longer refused — PostgreSQL supports both
  (upstream refused them for MySQL's sake); WITH stays refused;
- the bitwise XOR spelled ``^`` in ADQL renders as PostgreSQL's ``#``
  (``^`` is exponentiation in PostgreSQL — passing it through computed
  powers instead of XOR);
- hexadecimal literals render in decimal, which every PostgreSQL version
  accepts (bare ``0x…`` needs 16+);
- geometry-coordinate expressions are evaluated by a numeric-only parser
  (``_eval_number``) instead of upstream's ``eval()`` — the raw ``eval``
  ran arbitrary Python from the query string (e.g. a coordinate slot of
  ``eval('__import__("os").system(...)')`` executed on the worker).

Only the PostgreSQL output path is kept; upstream's MySQL branches were
dropped with the fork since this service never emits MySQL.
"""

import ast
import operator
import re

import antlr4
from antlr4.error.ErrorListener import ErrorListener
from queryparser.exceptions import QueryError, QuerySyntaxError

from .ADQLLexer import ADQLLexer
from .ADQLParser import ADQLParser
from .ADQLParserListener import ADQLParserListener
from .ADQLParserVisitor import ADQLParserVisitor

# Function names need to be recognized because whitespace between the name
# and the left parenthesis is not allowed and needs to be deleted.
adql_function_names = ('ABS', 'ACOS', 'ASIN', 'ATAN', 'ATAN2', 'CAST',
                       'CEILING', 'COALESCE', 'COS', 'DEGREES', 'EXP',
                       'FLOOR', 'LOG', 'LOG10', 'LOWER', 'MOD', 'PI',
                       'POWER', 'RADIANS', 'RAND', 'SIN', 'SQRT', 'TAN',
                       'TRUNCATE', 'UPPER')


def _removeFirstChild(ctx):
    if ctx.children is not None:
        del ctx.children[0]


def _remove_children(ctx, reverse=False):
    for _ in range(ctx.getChildCount() - 1):
        if reverse:
            _removeFirstChild(ctx)
        else:
            ctx.removeLastChild()


_NUM_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_NUM_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_number(expr):
    """Evaluate an arithmetic expression of numeric literals to a float.

    A safe replacement for upstream's ``float(eval(expr))``: only numeric
    constants and the ``+ - * / % **`` operators are permitted, so a
    coordinate slot cannot smuggle in a call, name or attribute access.
    Anything else raises ``ValueError``, which the caller treats the same
    way it treated ``eval`` failing — a non-numeric coordinate falls through
    to the column-name branch.
    """
    def _node(node):
        if isinstance(node, ast.Constant) and isinstance(
            node.value, (int, float)
        ) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _NUM_BINOPS:
            return _NUM_BINOPS[type(node.op)](_node(node.left), _node(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _NUM_UNARYOPS:
            return _NUM_UNARYOPS[type(node.op)](_node(node.operand))
        raise ValueError('non-numeric coordinate expression')

    return float(_node(ast.parse(expr, mode='eval').body))


def _convert_values(ctx, cidx):
    """
    Values inside the ADQL functions can be floats, expressions, or
    strings. Strings need to be treated differently because the sphere
    syntax differs slightly if we pass a column name instead of a value.
    """
    vals = []
    for i in ctx.children[cidx].getText().split(','):
        try:
            val = float(i)
        except ValueError:
            try:
                val = _eval_number(i)
            except (ValueError, SyntaxError, TypeError, ZeroDivisionError):
                val = '.'.join('{0}'.format(v) for v in i.split('.'))
        vals.append(val)
    return vals


def _get_ancestor_class_node(ctx, ancestor_class, depth=1):
    """ Returns the ancestor node at 'depth' level above the current node if
    the node is of type 'ancestor_class'. Otherwise, returns None
    """
    if not hasattr(ctx, 'parentCtx'):
        return None

    if depth > 1:
        return _get_ancestor_class_node(ctx.parentCtx, ancestor_class, depth-1)
    elif depth == 1:
        if isinstance(ctx.parentCtx, ancestor_class):
            return ctx.parentCtx
    return None


class SyntaxErrorListener(ErrorListener):
    def __init__(self):
        super(SyntaxErrorListener, self).__init__()
        self.syntax_errors = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        self.syntax_errors.append((line, column, offendingSymbol.text))


class ADQLGeometryTranslationVisitor(ADQLParserVisitor):
    """
    1) Find the rule we need to translate. Point, for example.
    2) After processing it, get rid of all off its children except the first
       one. This still keeps a token in the stream so it can be accessed later
       by other visitors or listeners. Otherwise is impossible (or hard?) to
       stick a new token in the stream so the walking works flawlessly.
    3) Hash the replacement string in the contexts dictionary. Use the context
       as a hash. This way we can return the hashed string instead the
       token so we effectively translate the rule.

    :param conunits:
        What should we be converting the units to. If no conversion is
        necessary, just pass an empty string.

    """
    def __init__(self, conunits="RADIANS"):
        self.contexts = {}
        self.conunits = conunits
        # Column references accepted in geometry slots (ADQL 2.1), as
        # written in the query. The translator has no column metadata, so a
        # caller that does (TAP_SCHEMA) uses this list to type-check them.
        self.geometry_columns = set()

    def _convert_values(self, ctx, cidx):
        return _convert_values(ctx, cidx)

    def visitRegular_identifier(self, ctx):
        if isinstance(ctx.parentCtx,
                      ADQLParser.User_defined_function_nameContext):
            return
        self.contexts[ctx] = ctx.getText()

    def visitSchema_name(self, ctx):
        self.contexts[ctx] = ctx.getText()

    def visitAs_clause(self, ctx):
        # We need to visit the AS clause to avoid aliases being treated same
        # as regular identifiers.
        try:
            ri = ctx.children[1].getText()
        except IndexError:
            ri = ctx.children[0].getText()

        _remove_children(ctx)
        self.contexts[ctx] = 'AS ' + ri

    def visitPoint(self, ctx):
        coords = []
        if len(ctx.children) > 4:
            for j in (2, 4):
                coords.extend(self._convert_values(ctx, j))
        else:
            coords.extend(self._convert_values(ctx, 2))

        if len(coords) == 3:
            coords = coords[1:]

        ctx_text = "spoint( %s(%s), %s(%s) )" % (self.conunits, coords[0],
                                                 self.conunits, coords[1])
        derived_column = _get_ancestor_class_node(
            ctx, ADQLParser.Derived_columnContext, depth=3)
        if derived_column is not None:
            ctx_text = f"spoint_to_array_deg({ctx_text})"
            if not (any([isinstance(child, ADQLParser.As_clauseContext)
                         for child in derived_column.children])):
                ctx_text = f"{ctx_text} AS adql_point"

        _remove_children(ctx)
        self.contexts[ctx] = ctx_text

    def visitBox(self, ctx):
        pars = []
        s = 4 if len(ctx.children) > 8 else 2
        pars.extend(self._convert_values(ctx, s))
        pars.extend(self._convert_values(ctx, s+2))
        pars.extend(self._convert_values(ctx, s+4))

        try:
            pos_cent_ra = float(pars[0])
            pos_cent_dec = float(pars[1])
            dra = float(pars[2])/2
            ddec = float(pars[3])/2
        except ValueError:
            raise QueryError('sbox values incorrect')

        ctx_text = "sbox( spoint(%s(%s),%s(%s)),spoint(%s(%s),%s(%s)) )" %\
            (self.conunits, '%.12f' % (pos_cent_ra - dra),
             self.conunits, '%.12f' % (pos_cent_dec - ddec),
             self.conunits, '%.12f' % (pos_cent_ra + dra),
             self.conunits, '%.12f' % (pos_cent_dec + ddec))

        _remove_children(ctx)
        self.contexts[ctx] = ctx_text

    def visitCircle(self, ctx):
        s = 4 if isinstance(ctx.children[2], ADQLParser.Coord_sysContext) else 2
        radius = self._convert_values(ctx, s+2)[0]
        circle_center = ctx.children[s]
        if isinstance(circle_center.children[0], ADQLParser.CoordinatesContext):
            point_parameters = self._convert_values(circle_center, 0)
            point_ctx_text = "spoint(%s(%s), %s(%s))" %\
                (self.conunits, point_parameters[0],
                 self.conunits, point_parameters[1])
        elif isinstance(circle_center.children[0].children[0],
                        ADQLParser.Column_referenceContext):
            # ADQL 2.1: a POINT-typed column as the circle centre.
            point_ctx_text = circle_center.children[0].getText()
            self.geometry_columns.add(point_ctx_text)
        else:
            point_ctx = circle_center.children[0].children[0].children[0]
            if isinstance(point_ctx, ADQLParser.PointContext):
                self.visitPoint(point_ctx)
                point_ctx_text = self.contexts[point_ctx]
            else:
                raise QueryError('In the current implementation, circle ' +
                                 'allows only explicitly defined point or a ' +
                                 'point column as the circle center. For ' +
                                 'instance, CIRCLE(POINT(t.ra, t.dec), 0.1)')

        ctx_text = "scircle( %s, %s(%s) )" %\
            (point_ctx_text, self.conunits, radius)
        derived_column = _get_ancestor_class_node(
            ctx, ADQLParser.Derived_columnContext, depth=3)
        if derived_column is not None:
            ctx_text = f"scircle_to_array_deg({ctx_text})"
            if not (any([isinstance(child, ADQLParser.As_clauseContext)
                         for child in derived_column.children])):
                ctx_text = f"{ctx_text} AS circle"
        _remove_children(ctx)
        self.contexts[ctx] = ctx_text

    def visitPolygon(self, ctx):
        pars = []

        for j in range(2, len(ctx.children), 2):
            par = self._convert_values(ctx, j)
            # only append coordinates
            # (ctx.children[2] can still return coord_sys which is deprecated)
            if len(par) > 1:
                pars.append(par)

        ustr = ''
        if self.conunits == "RADIANS":
            ustr = 'd'

        ctx_text = "spoly('{"
        for p in pars:
            ctx_text += '(%s%s,%s%s),' % (str(p[0]), ustr, str(p[1]), ustr)
        ctx_text = ctx_text[:-1] + "}')"
        derived_column = _get_ancestor_class_node(
            ctx, ADQLParser.Derived_columnContext, depth=3)
        if derived_column is not None:
            ctx_text = f"spoly_to_array_deg({ctx_text})"
            if not (any([isinstance(child, ADQLParser.As_clauseContext)
                         for child in derived_column.children])):
                ctx_text = f"{ctx_text} AS adql_polygon"

        _remove_children(ctx)
        self.contexts[ctx] = ctx_text


class ADQLFunctionsTranslationVisitor(ADQLParserVisitor):
    """
    Run this visitor after the geometry has already been processed.

    :param contexts:
        A dictionary that was created in the previous run and includes
        the replaced geometry chunks.

    """
    def __init__(self, contexts, conunits="DEGREES"):
        self.contexts = contexts
        self.conunits = conunits
        # See ADQLGeometryTranslationVisitor.geometry_columns.
        self.geometry_columns = set()

    def _geometry_argument(self, gve_ctx):
        """The SQL for one geometry_value_expression argument.

        A constructor was already translated by the geometry visitor and sits
        in the contexts dictionary; a column reference (ADQL 2.1) passes
        through as the column name, which is a pgsphere-typed column on the
        SQL side.
        """
        child = gve_ctx.children[0]
        try:
            return self.contexts[child]
        except KeyError:
            pass
        if isinstance(child, ADQLParser.Column_referenceContext):
            self.geometry_columns.add(child.getText())
            return child.getText()
        raise QueryError('unsupported geometry argument: %s' %
                         gve_ctx.getText())

    def visitArea(self, ctx):
        arg = self._geometry_argument(ctx.children[2])
        ctx_text = 'square_degrees(area(%s))' % arg
        derived_column = _get_ancestor_class_node(
            ctx, ADQLParser.Derived_columnContext, depth=9)
        if derived_column is not None:
            if not (any([isinstance(child, ADQLParser.As_clauseContext)
                         for child in derived_column.children])):
                ctx_text = f"{ctx_text} AS adql_area"

        for _ in range(ctx.getChildCount() - 1):
            ctx.removeLastChild()
        self.contexts[ctx] = ctx_text

    def visitCast_target(self, ctx):
        # The grammar admits the ADQL 2.1 geometry target types, but no
        # pgsphere mapping is defined for them, so they are a clear error
        # rather than SQL that fails in the database.
        if ctx.start.type in (ADQLParser.POINT, ADQLParser.CIRCLE,
                              ADQLParser.POLYGON):
            raise QueryError('CAST to a geometry type (%s) is not supported.'
                             % ctx.getText())

    def visitContains_predicate(self, ctx):
        comp_value_l = ctx.children[0].getText()
        comp_value_r = ctx.children[2].getText()
        if comp_value_l == '1' or comp_value_l == '0':
            self.visitContains(ctx.children[2])
            ctx_text = self.contexts[ctx.children[2]]
            if comp_value_l == '0':
                ctx_text = ctx_text.replace('@', '!@')
            _remove_children(ctx)
        elif comp_value_r == '1' or comp_value_r == '0':
            self.visitContains(ctx.children[0])
            ctx_text = self.contexts[ctx.children[0]]
            if comp_value_r == '0':
                ctx_text = ctx_text.replace('@', '!@')
            _remove_children(ctx, reverse=True)
        else:
            raise QueryError('The function CONTAINS allows comparison to '
                             '1 or 0 only.')

        self.contexts[ctx] = ctx_text

    def visitContains(self, ctx):
        arg = (self._geometry_argument(ctx.children[2]),
               self._geometry_argument(ctx.children[4]))
        ctx_text = '%s @ %s' % arg
        _remove_children(ctx)
        self.contexts[ctx] = ctx_text

    def visitDistance(self, ctx):
        arg = ('', '')
        if isinstance(ctx.children[2], ADQLParser.Coord_valueContext):
            arg = (self._point_argument(ctx.children[2]),
                   self._point_argument(ctx.children[4]))
        else:
            arg = (f"spoint(RADIANS({_convert_values(ctx, 2)[0]}), " +
                          f"RADIANS({_convert_values(ctx, 4)[0]}))",
                   f"spoint(RADIANS({_convert_values(ctx, 6)[0]}), " +
                          f"RADIANS({_convert_values(ctx, 8)[0]}))")

        ctx_text = '%s(%s <-> %s)' % ((self.conunits, ) + arg)
        derived_column = _get_ancestor_class_node(
            ctx, ADQLParser.Derived_columnContext, depth=9)
        if derived_column is not None:
            if not (any([isinstance(child, ADQLParser.As_clauseContext)
                         for child in derived_column.children])):
                ctx_text = f"{ctx_text} AS distance"

        for _ in range(ctx.getChildCount() - 1):
            ctx.removeLastChild()
        self.contexts[ctx] = ctx_text

    def _point_argument(self, coord_value_ctx):
        """The SQL for one coord_value argument of DISTANCE."""
        child = coord_value_ctx.children[0]
        if isinstance(child, ADQLParser.Column_referenceContext):
            # ADQL 2.1: a POINT-typed column.
            self.geometry_columns.add(child.getText())
            return child.getText()
        inner = child.children[0]
        if isinstance(inner, ADQLParser.PointContext):
            return self.contexts[inner]
        raise QueryError('Distance is possible only between two explicitly '
                         'defined points or point columns. For instance, '
                         'DISTANCE(POINT(t.ra, t.dec), POINT(0.0, 0.0)) '
                         'or DISTANCE(t.ra, t.dec, 0.0, 0.0)')

    def visitIntersects_predicate(self, ctx):
        comp_value_l = ctx.children[0].getText()
        comp_value_r = ctx.children[2].getText()
        if comp_value_l == '1' or comp_value_l == '0':
            self.visitIntersects(ctx.children[2])
            ctx_text = self.contexts[ctx.children[2]]
            if comp_value_l == '0':
                ctx_text = ctx_text.replace('&&', '!&&')
            _remove_children(ctx)
        elif comp_value_r == '1' or comp_value_r == '0':
            self.visitIntersects(ctx.children[0])
            ctx_text = self.contexts[ctx.children[0]]
            if comp_value_r == '0':
                ctx_text = ctx_text.replace('&&', '!&&')
            _remove_children(ctx, reverse=True)
        else:
            raise QueryError('The function INTERSECTS allows comparison to '
                             '1 or 0 only.')

        self.contexts[ctx] = ctx_text

    def visitIntersects(self, ctx):
        arg = (self._geometry_argument(ctx.children[2]),
               self._geometry_argument(ctx.children[4]))
        ctx_text = '%s && %s' % arg
        _remove_children(ctx)
        self.contexts[ctx] = ctx_text


class SelectQueryListener(ADQLParserListener):
    def __init__(self):
        self.limit_visitor = LimitVisitor()
        self.limit_contexts = {}

    def enterSelect_query(self, ctx):
        self.limit_visitor.visit(ctx)
        self.limit_contexts.update(self.limit_visitor.limit_contexts)


class LimitVisitor(ADQLParserVisitor):
    def __init__(self, remove=False):
        self.limit_terminal_visitor = LimitTerminalVisitor()
        self.limit_contexts = {}

    def visitSet_limit(self, ctx):
        try:
            self.limit = int(ctx.children[1].getText())
            ctx.removeLastChild()
            ctx.removeLastChild()

            self.limit_terminal_visitor.visit(ctx.parentCtx)
            lstr = 'LIMIT %d' % self.limit
            self.limit_contexts[self.limit_terminal_visitor.terminal] = lstr
        except IndexError:
            pass


class LimitTerminalVisitor(ADQLParserVisitor):
    def __init__(self):
        self.terminal = None

    def visitTerminal(self, ctx):
        self.terminal = ctx


class FormatListener(ADQLParserListener):
    """
    Used for formating the output query.

    """
    def __init__(self, parser, contexts, limit_contexts):
        self._parser = parser
        self.nodes = []
        self.contexts = contexts
        self.limit_contexts = limit_contexts

    def visitTerminal(self, node):
        # Upstream also refused INTERSECT and EXCEPT here for MySQL's sake;
        # PostgreSQL supports both, so this fork lets them through.
        try:
            if node.parentCtx.WITH():
                raise QueryError('WITH clause not supported.')

        except AttributeError:
            pass

        try:
            nd = self.contexts[node.parentCtx]
        except KeyError:
            nd = node.getText()
            if isinstance(node.parentCtx,
                          ADQLParser.Character_string_literalContext):
                if nd == "'":
                    nd = None
            elif isinstance(node.parentCtx,
                            ADQLParser.Bitwise_xorContext):
                # ADQL spells bitwise XOR '^'; in PostgreSQL '^' is
                # exponentiation and XOR is '#'.
                nd = '#'
            elif isinstance(node.parentCtx,
                            ADQLParser.Unsigned_hexadecimalContext):
                # Bare 0x… literals need PostgreSQL 16+; decimal is
                # equivalent and universal.
                nd = str(int(nd, 16))

        if nd is not None:
            if isinstance(node.parentCtx, ADQLParser.Set_function_typeContext)\
                or isinstance(node.parentCtx.parentCtx,
                              ADQLParser.User_defined_function_nameContext)\
                    or nd.upper() in adql_function_names:
                nd += '_'

            self.nodes.append(nd)

        try:
            nd = self.limit_contexts[node]
            self.nodes.append(nd)
        except KeyError:
            pass

    def format_query(self):
        query = ' '.join(self.nodes).rstrip(';')
        query = query.replace('_ ', '')
        query = query.replace(' . ', '.')
        query = query.replace(' , ', ', ')
        query = query.replace('( ', '(')
        query = query.replace(' )', ')')
        query = query.rstrip()
        return '%s;' % query.rstrip()


class ADQLQueryTranslator(object):
    """
    The main translator object used to do the actual translation.

    :param query:
        ADQL query string.

    """
    def __init__(self, query=None):
        self._query = None
        # Column references accepted in geometry slots (ADQL 2.1), as
        # written in the query; filled by to_postgresql(). The translator
        # has no column metadata, so a caller that does (TAP_SCHEMA) uses
        # this to refuse a non-geometry column before the database has to.
        self.geometry_columns = frozenset()

        if query is not None:
            self.set_query(query)

    def parse(self):
        """
        Parse the input query and store the output in self.tree.

        """
        inpt = antlr4.InputStream(self.query)
        lexer = ADQLLexer(inpt)
        self.stream = antlr4.CommonTokenStream(lexer)
        self.parser = ADQLParser(self.stream)
        self.syntax_error_listener = SyntaxErrorListener()
        self.parser._listeners = [self.syntax_error_listener]

        self.tree = self.parser.query()

        if len(self.syntax_error_listener.syntax_errors):
            raise QuerySyntaxError(self.syntax_error_listener.syntax_errors)

        self.walker = antlr4.ParseTreeWalker()

    @property
    def query(self):
        """
        Get the query string.

        """
        return self._query

    def set_query(self, query):
        """
        Set the query string. A semicolon is added in case it is missing.

        :param value:
            Query string.

        """
        self._query = query.lstrip('\n').rstrip().rstrip(';') + ';'
        self.parse()

    def translate(self, translator_visitor):

        select_query_listener = SelectQueryListener()
        self.walker.walk(select_query_listener, self.tree)

        format_listener = FormatListener(self.parser,
                                         translator_visitor.contexts,
                                         select_query_listener.limit_contexts)
        self.walker.walk(format_listener, self.tree)
        return format_listener.format_query()

    def to_postgresql(self):
        """
        Translate ADQL query to a PostgreSQL query using pg_sphere plugin
        for the spherical functions.

        """
        if self._query is None:
            raise QueryError('No query given.')

        self.parse()

        geometry_visitor = ADQLGeometryTranslationVisitor()
        geometry_visitor.visit(self.tree)
        translator_visitor = \
            ADQLFunctionsTranslationVisitor(geometry_visitor.contexts)
        translator_visitor.visit(self.tree)
        self.geometry_columns = frozenset(
            geometry_visitor.geometry_columns |
            translator_visitor.geometry_columns)

        translated_query = self.translate(translator_visitor)

        # Translate LOG10 to LOG and LOG to LN. It's not the most elegant
        # solution but it works.
        translated_query = re.sub(r'(?<=[\+\-\*/\(\s,])log\(', 'LN(',
                                  translated_query, flags=re.IGNORECASE)
        translated_query = re.sub(r'(?<=[\+\-\*/\(\s,])log10\(', 'LOG(',
                                  translated_query, flags=re.IGNORECASE)

        return translated_query
