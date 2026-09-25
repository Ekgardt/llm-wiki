# Every architecture mode is bounded

Date: 2026-09-25. Audit item B-33 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and by test)

- `get_architecture` without a mode (the summary) runs `code_graph` on the bounded workers
  (`_bounded_code_graph_call`: two slots, abandoned at the deadline). The modes `callers`,
  `callees`, `dependencies`, `path`, `community` and the symbol view called `code_graph` directly
  on the tool's thread, with no deadline; a live parse of a large tree held that thread, and four
  of them held every MCP tool slot.
- `code_graph`'s live extraction takes no deadline and cannot be interrupted from outside.

## Source

- Python docs, `concurrent.futures`, https://docs.python.org/3/library/concurrent.futures.html
  (fetched 2026-09-25), `Future.cancel()`: "If the call is currently being executed or finished
  running and cannot be cancelled then the method will return `False`". Work that cannot be
  cancelled can only be abandoned and capped, which is what the bounded workers do.

## Decision

- Every mode goes through the same bounded workers under the operation deadline
  (`_bounded_mode_query`), with the request's context copied into the worker; an overrun answers
  the named `code_graph_timeout` result the summary already gives, and at most two abandoned
  parses run at once.

## Files

- `scripts/mcp_server.py`
- `tests/test_every_architecture_mode_is_bounded.py`
- `CHANGELOG.md`
