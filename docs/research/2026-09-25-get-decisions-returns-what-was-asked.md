# get_decisions returns what was asked

Date: 2026-09-25. Audit item C-25 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `mcp_server._get_decisions` asked `search_memory.search` for exactly `limit` rows and
  kept the ones that are decisions afterwards. The rows are chunks of any page type, so
  concept pages and repeated chunks of one decision took the places: asking for 5 could
  return 2, or one decision twice.
- It returned the search's full internal row, while `recall` returns the agent row
  (`AGENT_ROW_FIELDS`, audit M7).

## Source

- Weaviate, "Filtering", https://docs.weaviate.io/weaviate/concepts/filtering (fetched
  2026-09-25), on filtering after the search: "If the filter is very restrictive, i.e. it
  matches only a small percentage of data points relative to the size of the data set,
  there is a chance that the original vector search does not contain any match at all."

## Decision

- Ask the search for `limit × CANDIDATE_FANOUT` rows (the same fan-out retrieval uses for
  page diversity, bounded by `MAX_SEARCH_LIMIT`), keep the first row of each decision page,
  return at most `limit`, shaped as the agent row plus `type`.

## Uncertainty

- A vault with fewer matching decisions than `limit` inside the widened pool still returns
  fewer; that is the true answer, not a filter artefact.

## Files

- `scripts/mcp_server.py`
- `tests/test_get_decisions_returns_what_was_asked.py`
- `CHANGELOG.md`
