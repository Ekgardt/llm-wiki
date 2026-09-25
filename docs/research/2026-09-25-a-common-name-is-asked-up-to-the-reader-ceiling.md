# A common name is asked up to the reader's ceiling

Date: 2026-09-25. Audit item C-36 (docs/AUDIT-2026-09-25-full.md).

## Fact
- `EvidenceGraph._execute` (scripts/evidence_graph.py) fetches `max_rows + 1` rows
  and raises `ValueError("Evidence Graph query row ceiling exceeded")` when more
  match: `max_rows` on `find_nodes` is a refusal, not a cut.
- `docs/research/2026-09-17-a-name-is-resolved-before-the-graph-is-asked.md` fixed
  this class for the call walks: `_named_node_ids` asks up to `SEED_LOOKUP_ROWS`
  (the reader's `MAX_ROWS`, 10 000) and cuts on our side.
- Four lookups by name still pass a small bound as `max_rows`:
  - `code_graph._stored_symbol_node_ids` (community mode with a symbol): 512.
    `mode=community symbol=__init__` is refused on a repository with more than
    512 same-name definitions.
  - `provenance_join._graph_locations`: 5 — the provenance join refuses any name
    with more than five nodes, and the MCP tool reports an error.
  - `symbol_snippet._matching_nodes`: 200, filtered by owner *after* the query, so
    the answer's own advice ("qualify it as owner.name") cannot work for a name
    with more than 200 definitions.
  - `trace_ingest._trace_caller_fields`: `MAX_NODE_FILTER` (512), so the reader's
    anonymous ceiling error pre-empts the named `trace_target_filter_too_wide`
    refusal the module raises right after.

## Source (fetched 2026-09-25)
SQLite, "SELECT", section 5 "The LIMIT clause", https://www.sqlite.org/lang_select.html:
"The LIMIT clause is used to place an upper bound on the number of rows returned
by the entire SELECT statement." ... "the SELECT returns the first N rows of its
result set only". A LIMIT bounds what is read; whether exceeding it is an error is
our reader's choice, which is why the ceiling must be the reader's and the cut ours.

## Decision
Each of the four asks up to `evidence_graph.MAX_ROWS` and applies its own bound
afterwards: community reuses `_named_node_ids`; provenance keeps the first five
locations and reports `location_count`/`locations_truncated`; the snippet filters
by owner first and keeps its "qualify it" error for more than 200 matches; trace
callers lets `_require_target_bound` name its refusal.

## Uncertainty
A name with more than 10 000 nodes is still refused by the reader; that is the
absurdity ceiling and is not changed.

## Files
- scripts/code_graph.py
- scripts/provenance_join.py
- scripts/symbol_snippet.py
- scripts/trace_ingest.py
- tests/test_a_name_is_resolved_before_the_graph_is_asked.py
