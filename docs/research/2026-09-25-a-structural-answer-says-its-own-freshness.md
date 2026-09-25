# A structural answer says its own freshness

Date: 2026-09-25. Audit item B-34 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `mcp_server._graph_component_freshness` returned `fresh` for every structural answer without an
  `error`, while the same answer's `freshness` block could say `stale_by_commit: true` (the
  generation was built from an older commit of the checkout).
- `mcp_contract._source_commit` was an `lru_cache` over the vault root, so an MCP server started
  before the nightly fast-forward kept naming the old commit for its whole life.

## Source

- RFC 9111, https://www.rfc-editor.org/rfc/rfc9111 (fetched 2026-09-25), section 4.2: "A 'fresh'
  response is one whose age has not yet exceeded its freshness lifetime." A generation built from
  an earlier commit of a checkout that has moved has exceeded it.

## Decision

- The graph component takes the answer's own block: `stale` when `stale_by_commit` is true,
  `fresh` when it is false, `unknown` when the block is missing or says the generation was
  unreadable (no commit could be compared; this was reported `fresh`).
- The source commit is re-read after 5 s (`SOURCE_COMMIT_TTL_SECONDS`), one `git rev-parse` per
  window.

## Files

- `scripts/mcp_server.py`
- `scripts/mcp_contract.py`
- `tests/test_a_structural_answer_says_its_own_freshness.py`
- `tests/test_mcp_contract.py`
- `tests/test_mcp_server.py`
- `CHANGELOG.md`
