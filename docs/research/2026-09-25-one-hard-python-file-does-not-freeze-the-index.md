# One hard Python file does not freeze the index

Date: 2026-09-25. Audit item A-14 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (reproduced here, Python 3.14.7; callers from the code graph)

- `ast.parse` of `x = 1+1+…` with 100,000 terms raises `RecursionError: Stack overflow (used
  8152 kB) during compilation`; with 10,000 terms it parses, and a recursive `ast.NodeVisitor`
  then raises `RecursionError`. The code extractor's own walks survive a 2,000-deep chain.
- The readers of repository code caught different subsets: `code_extractor` (SyntaxError,
  ValueError, UnicodeError), `import_resolver` and `code_graph` (SyntaxError only),
  `path_coverage` (SyntaxError, ValueError, UnicodeError), `value_references` and
  `fresh_positions` (with RecursionError). An escaping `RecursionError` ended the whole repository
  refresh, and it failed the same way on every retry.
- `import_resolver.resolve_python_imports_and_calls` is called only from `code_graph`
  (`_regex_parse_python`, `_language_symbols`; graph query 2026-09-25).

## Source

- Python docs, `ast.parse`, https://docs.python.org/3/library/ast.html (fetched 2026-09-25):
  "It is possible to crash the Python interpreter with a sufficiently large/complex string due to
  stack depth limitations in Python's AST compiler."

## Decision

- One definition, `python_parse.PARSE_FAILURES` = SyntaxError, ValueError, UnicodeError,
  RecursionError, MemoryError, used by every reader of repository Python; the file becomes that
  reader's parse error (`parse_error` observation, empty imports, an error in the parse probe) and
  the other files are still read.
- `import_resolver` also treats a `RecursionError` from its recursive visitor as an unreadable file.
- Modules loaded both as `scripts.<name>` and by name import it with the existing dual form.

## Uncertainty

- A C-level stack overflow in the parser can in principle abort the process instead of raising; on
  this interpreter it raised. That case is outside what a Python `except` can catch.

## Files

- `scripts/python_parse.py`
- `scripts/code_extractor.py`
- `scripts/import_resolver.py`
- `scripts/code_graph.py`
- `scripts/value_references.py`
- `scripts/path_coverage.py`
- `scripts/fresh_positions.py`
- `tests/test_one_hard_python_file_does_not_freeze_the_index.py`
- `CHANGELOG.md`
