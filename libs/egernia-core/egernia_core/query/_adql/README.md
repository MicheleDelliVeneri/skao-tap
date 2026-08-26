# Vendored ADQL 2.1 parser (fork of queryparser)

This directory is a fork of the ADQL dialect of
[queryparser](https://github.com/aipescience/queryparser) 0.7.4
(Apache-2.0, AIP), extended to ADQL 2.1. It exists because upstream's
grammar predates 2.1 — most visibly, a geometry-typed *column* as an
`INTERSECTS`/`CONTAINS` argument was a syntax error, which this service
used to paper over with a sentinel-literal substitution — and upstream has
not moved (0.7.4 is the latest release).

The decision to fork rather than replace the parser is recorded in
`docs/roadmap.md` (package 21): there is no maintained Python alternative
that parses ADQL 2.1, and the service's translation hot path (SLL
prediction, single parse — package 18) is built on this exact
ANTLR stack, so the fork keeps that evidence valid.

## Contents

- `ADQLLexer.g4`, `ADQLParser.g4` — the forked grammar (source of truth).
- `ADQLLexer.py`, `ADQLParser.py`, `ADQLParserListener.py`,
  `ADQLParserVisitor.py` — **generated** from the grammar by ANTLR 4.13.1;
  do not edit by hand.
- `translator.py` — the tree-walking translator, forked from upstream's
  `adqltranslator.py`; its module docstring lists every behavioural change.

## Grammar diff against upstream 0.7.4

- `CSL` (string literal) accepts the empty string — ADQL 2.1 deprecates the
  coordinate-system argument and clients write `POINT('', ra, dec)`.
- `BIGINT` token added (used only by `cast_target`).
- `geometry_value_expression` accepts a `column_reference` alternative —
  the ADQL 2.1 core-grammar change that lets footprint columns be queried
  directly.
- `char_function` is `( LOWER | UPPER ) ( character_value_expression )`
  instead of `LOWER ( character_string_literal )`.
- New rules `cast_specification`, `cast_target`, `coalesce_expression`,
  wired into `value_expression_primary`.

## Regenerating after a grammar change

The generated files must match the `antlr4-python3-runtime` version pinned
in `uv.lock` (currently 4.13.1). With Java available (`pip install
antlr4-tools` bootstraps one):

```sh
cd libs/egernia-core/egernia_core/query/_adql
antlr4 -v 4.13.1 -Dlanguage=Python3 -lib . ADQLLexer.g4
antlr4 -v 4.13.1 -Dlanguage=Python3 -visitor -lib . ADQLParser.g4
rm -f ADQLLexer.tokens ADQLLexer.interp ADQLParser.tokens ADQLParser.interp
```

Then run the conformance tests (`tests/unit/test_adql.py`) and the hot-path
benchmark (`tests/benchmarks/test_hot_paths.py`) — the SLL fast path's
"translates exactly as the full-context parse" property is the regression
guard for any grammar change.

The rest of queryparser (the PostgreSQL processor used by
`touched_tables`, and the exceptions module) is still consumed from the
PyPI package; only the ADQL dialect is forked.
