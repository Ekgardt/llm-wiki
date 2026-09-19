# A name is resolved before the graph is asked

Date: 2026-09-17. Audit 3, code intelligence, findings A5, A6, A7 and the silent
seed cut named in B29.

Files: `scripts/code_graph.py`, `scripts/code_hints.py`,
`tests/test_a_name_is_resolved_before_the_graph_is_asked.py`

## What was found

`get_architecture` hands the graph facades a *name*. Three places still treat
what they were given, or what they resolved, as if the graph's reader bounds
did not exist.

1. **A5, `mode=path`.** `find_paths` passes both names straight to
   `EvidenceGraph.path`, which expects node ids. A name that fits the id syntax
   matches no node and answers `paths: []`; a private name (`_x`) raises
   `ValueError: source_node_id must use the closed delimiter-safe identifier
   syntax`. The same defect was fixed for `dependencies` (CODE-07, the docstring
   of `_dependency_seed_nodes`); `path` was left behind. The existing test hides
   it because its fixture's node ids are literally `caller` and `callee`.
2. **A6, common names.** `_dependency_seed_nodes` asks `find_nodes` for at most
   `DEPENDENCY_SEED_LIMIT = 20` rows, and `find_nodes` *refuses* when more match
   ("Evidence Graph query row ceiling exceeded"), so the later
   `[:FLOW_MAX_SEEDS]` never gets a chance. `dependencies`, `data_flow` and
   `cross_service` raise for `main`, `__init__`, `close`, `run` — 115, 417, 93
   and 61 definitions in this repository. `_walk_seeds` already does it the
   other way round (ask wide, cut to 20) but cuts without saying so.
3. **A7, id lists over 512.** `EvidenceGraph.edges` and its siblings refuse more
   than `MAX_NODE_FILTER = 512` node ids. `_store_find_callers` and
   `_store_find_callees` pass every same-name node (up to 10 000), the flow and
   service walks pass a whole frontier (up to 1 000), and `code_hints` passes
   every route (up to 5 000). Only `_walk_locations` chunks.

## Sources

- SQLite limits (https://www.sqlite.org/limits.html), "Maximum Number Of Host
  Parameters In A Single SQL Statement": the default
  `SQLITE_MAX_VARIABLE_NUMBER` was 999 before 3.32.0 and is 32766 since. That
  floor is why `evidence_graph.MAX_NODE_FILTER` is 512, as its own comment
  says; a caller with more ids has to ask in slices, the reader will not grow.
- The repository's own precedent, `code_graph._walk_locations`: ids are sent in
  `_LOCATION_CHUNK = 512` slices and the answers merged.

## Alternatives

1. Raise the reader's bounds. They belong to another area, are sized from a
   measured SQLite floor, and a larger bound only moves the cliff.
2. Catch the refusal and answer empty. That is the silent wrong answer CODE-07
   and NEW-124 were written against.
3. Resolve and slice on the caller's side: ask for names up to the reader's own
   row ceiling, cut to the seed limit in one place and say so in the report;
   resolve both ends of a path the same way; send every id list in 512-id
   slices through one helper.

## Decision

Alternative 3.

- One seed helper: names are looked up with `max_rows=10_000`, sorted, cut to
  the mode's seed limit; when the cut drops anything the report carries
  `matching_symbol_nodes` and `symbol_nodes_truncated: true`. Nothing is added
  to an answer that was not cut.
- `find_paths` resolves source and target with the dependency resolver (name,
  then path, then node id), tries at most `PATH_MAX_ENDPOINTS = 5` nodes per
  end, stops at ten paths, and reports how many nodes each end resolved to.
- One chunk helper, `_in_node_chunks`, used by every reader call that passes a
  caller-sized id list. Row ceilings stay per call, so a merged answer can be
  larger than one call's ceiling but each slice is still refused by name if it
  alone overflows.
