# The context tool says what `include` does

Date: 2026-09-25. Audit item C-20 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and history)

- `get_context`'s schema says: "'frontmatter' adds content_preview for backward
  compatibility". Nothing adds it. `git log -S content_preview` shows the preview
  (`page["content"][:500]`) was removed on 2026-07-18 (`939046bb`) when the
  context compiler replaced the per-page lookup; the description stayed.
- The compiled answer already carries the pages' packed text in `text`, under
  the token budget; a 500-character preview beside it would repeat what the
  client has.

## Source

- Model Context Protocol specification, tools,
  https://modelcontextprotocol.io/specification/2025-06-18/server/tools
  (fetched 2026-09-25): a tool carries "`description`: Human-readable description
  of functionality" and "`inputSchema`: JSON Schema defining expected
  parameters". The model reads the description to decide what to pass; a
  description of a parameter that does nothing costs it a guess.

## Decision

- `include` stays accepted, so old clients do not fail validation, and its
  description says it is accepted for compatibility and changes nothing: the
  page content is in `text`. No preview is reintroduced.

## Files

- `scripts/mcp_server.py`
- `tests/test_mcp_server.py`
- `CHANGELOG.md`
