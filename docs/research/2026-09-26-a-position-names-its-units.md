# A position names its units

Date: 2026-09-26. Audit 2026-09-26, finding C-9 (two of its parts).

## What was wrong

Facts, at a2965838:

- The MCP input schema for `get_architecture` declared `character` as
  `{"type": "integer", "minimum": 0}` and nothing else, while
  `lsp_positions.SourceDocument.validate_anchor` reads it as a UTF-8 byte offset.
  An agent that counts UTF-16 units (the LSP default) is off by one per non-ASCII
  character before the cursor, and nothing told it.
- `validate_anchor` refused `character` past the end of the line
  ("character is outside the line"); the LSP specification clamps it.

## Decision

- `validate_anchor` clamps a `character` past the line end to the line end. A
  character inside a multi-byte code point is still refused. Anchors a stored
  edge carries are still compared exactly (`_edge_anchors_valid` requires
  equality), so a stored out-of-range anchor is still rejected.
- The schema describes `line` as 1-based and `character` as a 0-based UTF-8 byte
  offset; docs/CODE-NAVIGATION.md says the same.
- Guard: `tests/test_a_position_names_its_units.py` fails if any tool's input
  schema has a `line`, `character` or `column` property whose description does
  not name its base.

## Source

Language Server Protocol 3.17 specification, `Position`, fetched 2026-09-26 from
https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/:

> Character offset on a line in a document (zero-based). The meaning of this
> offset is determined by the negotiated `PositionEncodingKind`.
>
> If the character value is greater than the line length it defaults back
> to the line length.

## Files

- `scripts/lsp_positions.py`
- `scripts/mcp_server.py`
- `tests/test_a_position_names_its_units.py`
- `tests/test_lsp_positions.py`
- `docs/CODE-NAVIGATION.md`
