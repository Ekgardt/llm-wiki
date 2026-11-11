# A symbol snippet keeps its definition

Research and implementation date: 2026-10-02.

The source reader cut every definition after 120 lines. The module explicitly
called the basis unknown. A final return beyond that line was absent even when
the indexed occurrence or fresh parser supplied the exact ending. The heuristic
reader also called an exactly 120-line complete function truncated. All three
read paths shared this display cut, so the correction belongs in the shared
snippet module rather than one MCP mode.

## Sources and choice

Three independent primary sources were checked on the research date:

- [Python 3.10 AST documentation](https://docs.python.org/3.10/library/ast.html#ast.get_source_segment)
  defines source extraction by the recorded start and end positions. The chosen
  implementation keeps compatibility with the product's Python 3.10 baseline.
- [Microsoft LSP 3.17 metamodel](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/metaModel/metaModel.json)
  distinguishes a symbol's enclosing range from its selection range. A source
  block and a position used to reveal a name have different purposes.
- [MCP 2025-11-25 tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
  defines tool content and output-schema validation. It does not prescribe a
  120-line source cut. This specification is cited for result semantics; the
  patch changes no negotiated protocol version.

Keep the whole recorded definition, or the whole existing indentation-derived
block when no occurrence exists. Keep the existing file-read admission and the
response-level MCP budget. The latter already reports row omission or refuses an
answer that cannot fit; it must not silently cut a definition's source string.
The heuristic result remains explicitly heuristic. This is an internal reader
correction: no structure, runtime path, environment contract, schema or tool is
added or changed.

Alternatives considered: retain the unexplained display cut; add another caller
knob for it; or add a continuation interface. The first leaves missing behavior,
the second gives an arbitrary number a new location, and the third duplicates
the existing response-budget boundary without a demonstrated need. Longer
definitions now spend more response tokens when their complete body is needed.
That is necessary context, not a demonstrated token saving. Whole-cycle answer
quality and cost qualification remains open in the audit.

## Evidence and limits

New tests use complete functions with a required final return at 120, 121 and
301 lines. They exercise recorded ranges, actual file reads, and fresh AST spans.
The first fixture run exposed a missing test node identifier as well as real
cuts; the corrected original-code run was **7 failed / 2 passed**, and both logs
are retained. Existing cut assertions are replaced with exact whole-body and
range assertions. A separate check passes a complete 301-line body through the
actual MCP response shaper.

The first related green run before that final shaper check was **77 passed**.
Final qualification and installed proof are recorded separately in the private
audit progress page; this research does not claim an unfinished full suite passed.

The old line-count constant and all four read-path uses are removed. Dated older
research and inventory tables remain historical evidence, not current limits.
Other file, graph and display ceilings remain under the separate law-9 review;
this change does not establish their basis or close the entire audit item.

Evidence: `logs/audit-2026-10-02-whole-snippet-architecture.json`,
`logs/audit-2026-10-02-whole-snippet-red.txt`,
`logs/audit-2026-10-02-whole-snippet-red-corrected.txt`,
`logs/audit-2026-10-02-whole-snippet-green.txt`.
