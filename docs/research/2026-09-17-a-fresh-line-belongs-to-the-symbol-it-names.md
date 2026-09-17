# A fresh line belongs to the symbol it names

Date: 2026-09-17. Audit 3, code intelligence, findings A3, A4 and the bare-name
half of B34.

Files: `scripts/fresh_positions.py`, `scripts/symbol_snippet.py`,
`scripts/mcp_server.py`, `tests/test_a_fresh_line_belongs_to_the_symbol_it_names.py`

## What was found

The 2026-09-13 decision
(`docs/research/2026-09-13-a-shorter-answer-and-a-fresher-line.md`) corrects a
stored line from the file on the answer path: "the rows that name symbols in it
get the line those symbols occupy now". Two places apply it to a row or a name
that is not the symbol the line belongs to.

1. **A3.** `mcp_server._POSITION_KEYS` lists `callers` and `unresolved_callers`.
   In a one-hop answer (stored, live, or unresolved) the row's `line` is the
   *call site* and its `qualified_name` is the *caller*. `_corrected` replaces
   the call-site line with the caller's definition line and labels it
   `line_read_from: "file"`. Reproduced 2026-09-17:
   `{"line": 6, "qualified_name": "caller"}` became `line: 4`. A caller's body
   always starts after its `def`, so this is every one-hop caller row whose file
   parses, not an edge case. Only the depth walk (`depth_applied` in the report)
   builds rows from the caller node's own location, where the correction is
   right. Callee rows carry no name key and were never touched.
2. **A4 / B34.** `symbol_snippet._file_block` passes the bare `metadata["name"]`,
   and `fresh_positions` keeps the *first* definition of a bare name
   (`setdefault`). With `A.run` and `B.run` in a stale file, the snippet for
   `B.run` is the source of `A.run`, marked `precision: "exact"`. The same
   `setdefault` lets a method that comes first shadow a module-level function of
   the same name, in both the span and the line tables.

## Sources

- Python `ast` documentation (https://docs.python.org/3/library/ast.html):
  nodes carry `lineno` and `end_lineno`, "the first and last line numbers of
  source text span (1-indexed ...)". The definition line of a caller and the
  line of a call inside it are different nodes' `lineno`; one cannot stand for
  the other.
- The repository's own contract for the walk, `code_graph.find_callers`
  docstring: "`max_depth` above 1 walks the generation's CALLS closure that deep
  (issue #24, B4); rows then carry `depth`, and the report `depth_applied`".

## Alternatives

1. Shift a call-site line by how far the caller's definition moved. Needs the
   indexed definition line, which the row does not carry, and is still a guess
   when the body itself was edited.
2. Guess the row kind from its fields inside `fresh_positions`. The module would
   then know the shapes of another module's rows.
3. Let the answer say what its rows are: the report's `depth_applied` is present
   exactly when rows were built from node locations. Refresh `callers`/`callees`
   only then; never refresh `unresolved_callers`. For names: look a method up
   under its owner and nowhere else, and let a module-level definition own its
   bare name.

## Decision

Alternative 3. A one-hop call-site line stays the stored line: possibly a day
old, never a different line presented as read from disk. When the owner is
known and `Owner.name` is gone from the file, the snippet keeps the stored block
instead of borrowing a namesake.
