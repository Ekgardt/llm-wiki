# One name is asked past the candidate bound

Date: 2026-09-26. Audit 2026-09-26 B-7.

## Facts

- `code_graph._stored_dead_nodes` read every dead-code candidate with
  `EvidenceGraph.nodes_without_edges(max_rows=10_000)`, which raises
  "Evidence Graph query row ceiling exceeded" past the bound. A question about
  one symbol filtered by name only after that read, so on a graph with more than
  10 000 candidates `find_dead_code(symbol=…)` failed as well, uncaught.
- The graph already narrows by a metadata field in SQL
  (`_metadata_clause` → `json_extract(metadata_json, '$.name') = ?`) and already
  has a bounded top-N reader that states its own cut (`_execute_top`).
- SQLite JSON functions (https://www.sqlite.org/json1.html, fetched 2026-09-26):
  "The json_extract(X,P1,P2,...) extracts and returns one or more values from the
  well-formed JSON at X", and they "are built into SQLite by default, as of SQLite
  version 3.38.0".

## Decision

- `EvidenceGraph.nodes_without_edges` takes an optional `name`, narrowed in SQL,
  and returns `(nodes, truncated)` through `_execute_top`: past the bound it
  returns the first rows and says so. It was the only caller's shape, so the old
  refusing form is removed rather than kept beside it.
- `find_dead_code` passes the symbol's last component as `name` and reports
  `candidates_truncated` in its counts.

## Files

- `scripts/evidence_graph.py`
- `scripts/code_graph.py`
- `tests/test_whole_graph_aggregates.py`
- `tests/test_one_name_is_asked_past_the_candidate_bound.py`
- `CHANGELOG.md`
