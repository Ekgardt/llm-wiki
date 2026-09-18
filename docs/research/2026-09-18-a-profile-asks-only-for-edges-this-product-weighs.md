# A profile asks only for edges this product weighs

Dated 2026-09-18. Merge integration: the co-change island was removed in one
worktree while the retrieval profiles that named it were read in another.

Files: `scripts/retrieval.py`, `tests/test_graph_retrieval.py`

## What was found

`docs/research/2026-09-17-graph-three-islands-nothing-sails-to.md`, decision C3,
deleted the `CO_CHANGED_WITH` edge: no producer anywhere in the product, no
source span to hang an assertion on. It removed the weight from
`retrieval.GRAPH_EDGE_DECAY` — "one line, in another area's file, named in the
report" — and stopped there. The same name also stood in
`GRAPH_PROFILE_EDGE_TYPES["IMPACT"]`, the list of edge types the IMPACT profile
asks the graph backend for.

The consequence was not a missing edge. `_run_graph_backend` builds the decay
map as `{edge: GRAPH_EDGE_DECAY[edge] for edge in edge_types}` — and built it
*inside* the `try` that guards the backend call. So every IMPACT retrieval
raised `KeyError('CO_CHANGED_WITH')` before the backend was ever called, the
broad `except Exception` read it as "a broken graph degrades one signal only",
and the answer came back as `BASE` with `fallback_reason="graph_error"`. IMPACT
had silently stopped being a graph profile. The failing test
(`test_all_profiles_request_declared_signals_behaviorally[IMPACT]`) reported
only `assert 'BASE' == 'IMPACT'`, because the real error had been swallowed.

A second test, `test_edge_family_ablation_removes_disabled_family_before_fusion`,
used `CO_CHANGED_WITH` as the family it disables, and the validator
`_edge_families_are_known` correctly refused a name that is no longer an edge
type. That test was the stale side.

## Practice on this date

- Python documentation,
  [`try` statement](https://docs.python.org/3/reference/compound_stmts.html#the-try-statement)
  (fetched 2026-09-18): "The `except` clause(s) specify one or more exception
  handlers. When no exception occurs in the `try` clause, no exception handler is
  executed." The handler covers whatever the clause contains — so what a `try`
  contains is a decision, not an accident. Only the call that may fail for
  reasons outside this product belongs inside a handler that degrades a signal.
- The repository's own rule against this shape, `CLAUDE.md` and the round's
  brief: no broad `except`, and no reading of a programming error as an
  environmental one. `_active_evidence_graph` was corrected the same way on
  2026-09-17 (audit 3, B26) when it caught `TypeError` and answered "this
  repository has no generation".

## The decision

- `CO_CHANGED_WITH` leaves `GRAPH_PROFILE_EDGE_TYPES["IMPACT"]`, finishing the
  C3 removal. A comment in the table says every name in it must be a key of
  `GRAPH_EDGE_DECAY`.
- The decay map is built **before** the `try`, so a profile naming an unweighed
  edge type raises where it happens instead of being reported as a backend
  failure. The handler keeps guarding exactly what it was written for: the
  backend call.
- `tests/test_graph_retrieval.py` gains
  `test_every_profile_asks_only_for_edges_this_product_weighs`, which compares
  every profile's declared edge types against the decay table, so the next edge
  type to be retired cannot be half-removed. The ablation test now disables
  `LINKS_TO`, an edge family this product still has.
