# Python 3.10 does not crash on a deep expression

Date: 2026-09-26. CI run on PR 43, commit 5fd2b2fc, job windows_full py3.10 shard 1.

## Fact
- The job died with "Windows fatal exception: stack overflow" inside `ast.parse`,
  called from `code_extractor._parsed_python`, while parsing the audit A-14 test's
  generated file (`x = 1+1+…`, 100 000 terms). No Python exception is raised: the
  whole process ends, so no handler (`python_parse.PARSE_FAILURES`) can help.
- Measured 2026-09-26, `ast.parse` in a thread with a 1 MiB stack (the Windows
  main-thread default), per interpreter:
  - 3.10: ok at 15 000 terms, the process is killed (exit 139) at 17 000;
  - 3.11: `RecursionError` from 3 000 terms;
  - 3.12–3.14: ok at 3 000, `RecursionError` at 10 000.
  Linux passed only because its main thread has an 8 MiB stack.
- Seven readers of repository code call `ast.parse` directly: `code_extractor`,
  `code_graph`, `fresh_positions` (twice), `import_resolver`, `path_coverage`,
  `value_references`. One generated file in an indexed repository could kill the
  indexer or the MCP server on Windows with Python 3.10.

## Source (fetched 2026-09-26)
Python 3.10 documentation, `ast.parse`, https://docs.python.org/3.10/library/ast.html:
"Warning: It is possible to crash the Python interpreter with a sufficiently
large/complex string due to stack depth limitations in Python's AST compiler."

## Decision
One entry point, `python_parse.parse_python(source, filename)`, used by all seven
readers. On Python 3.10 only, it first counts, per logical line (tokenize), the
operator tokens that nest in the tree (everything but brackets, commas, colons,
dots and `=`), and refuses a line with more than 3 000 of them with
`RecursionError`, the same answer 3.11 gives at that size and five times below
the measured crash. On 3.11 and later it is `ast.parse` unchanged. A source the
tokenizer rejects goes to `ast.parse`, which reports it as a `SyntaxError`.

## Cost (measured 2026-09-26, Python 3.10, this repository's 178 scripts, 6.8 MB)
`ast.parse` alone 2.66 s; with the guard and a tokenize pass on every file 5.36 s;
with a first check that skips files holding no more operator characters than the
limit, 3.40 s (+28 %). Python 3.11 and later pay nothing.

## Uncertainty
The count is a proxy for the parser's depth; nesting that is not an operator
chain (`[[[[…` is capped at 200 levels by the 3.10 tokenizer) is not counted.
The crash boundary was measured on Linux with a 1 MiB thread, not on Windows.

## Files
- scripts/python_parse.py
- scripts/code_extractor.py
- scripts/code_graph.py
- scripts/fresh_positions.py
- scripts/import_resolver.py
- scripts/path_coverage.py
- scripts/value_references.py
- tests/test_python_3_10_does_not_crash_on_a_deep_expression.py
