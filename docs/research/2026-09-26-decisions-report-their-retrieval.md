# Decisions report their retrieval

Date: 2026-09-26. Audit 2026-09-26, finding C-10 (`get_decisions` hides
degradation and freshness).

## What was wrong

`recall` reached the corpus through `_search_vault`: a deadline with a reserved
second for the lexical fallback, a trace sink, and an envelope whose quality,
fallback flag, answer generation and per-signal freshness were read from that
trace. `get_decisions` called `search_memory.search` itself, with no trace sink
and no fallback reserve, and returned a bare list; the envelope read quality from
the rows alone and had no freshness component. A decision answer served from the
lexical leg because vectors were missing said nothing about it.

## Decision

- `get_decisions` goes through `_search_vault` with its own caller
  (`SearchCaller("mcp.get_decisions", False)`: it keeps recording its own
  one-row-per-page impressions, not the search's).
- Its data is `{"results", "retrieval_trace", "_meta"}`, the shape `recall`
  returns; quality, fallback, answer generation and freshness components come from
  the trace for every tool in `RETRIEVAL_TOOLS`.
- Guard: a test parses `mcp_server.py` and fails if any function other than
  `_run_vault_search` and the warm-up imports `search` from `search_memory`, so a
  new tool cannot reach the corpus around the planner.
- `docs/USER-GUIDE.md` names the new shape.

## Source

Model Context Protocol specification, Tools, revision 2025-11-25, fetched
2026-09-26 from https://modelcontextprotocol.io/specification/2025-11-25/server/tools:

- "**Structured** content is returned as a JSON object in the `structuredContent`
  field of a result."
- "If an output schema is provided: Servers **MUST** provide structured results
  that conform to this schema. Clients **SHOULD** validate structured results
  against this schema."

Fact from the code: every tool's answer is already wrapped in one envelope object
(`mcp_contract.build_envelope`), and the list sat in its `data` field, so the
protocol was not violated. Conclusion (mine): the quality fields the envelope
reports are what a client validates and trusts, so they must be computed from what
the retrieval actually did; one data shape for both retrieval tools lets one path
compute them.

## Files

- `scripts/mcp_server.py`
- `tests/test_decisions_report_their_retrieval.py`
- `tests/test_mcp_server.py` (a test fake takes the trace sink)
- `docs/USER-GUIDE.md`
