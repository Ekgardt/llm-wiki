# Recall asks the graph about relations

Date: 2026-09-25. Audit item C-22 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `retrieval._requested_profile` returned `HYBRID` for every semantic search with no
  explicit profile, so `PROFILE_SIGNALS["HYBRID"] == ("lexical", "dense")` was the plan of
  every `recall` and CLI search: the evidence-graph leg (`_run_graph_stage`) never ran for
  them, although the planner (`analyze_query`) recognises relation questions ("depends on",
  "callers", "related to", …) and recommends `GRAPH`, `REPO_MAP`, `IMPACT` or `GLOBAL`.
- Grounded recall (`query_memory._resolved_profile`) and the LongMemEval stand
  (`benchmark/longmemeval_vault.profile_for`) do follow the planner, so the plain `recall`
  path ran a different plan from the one measured.
- When no trace was reported, `mcp_server._unreported_trace` named the planner's
  recommendation as the requested mode, not the `HYBRID` that the search ran.
- `retrieve()` recomputed the wanted signals from the profile name, so a caller could not
  ask for a profile's graph together with dense.

## Source

- Microsoft GraphRAG, "Local Search", https://microsoft.github.io/graphrag/query/local_search/
  (fetched 2026-09-25): "The local search method combines structured data from the knowledge
  graph with unstructured data from the input documents to augment the LLM context with
  relevant entity information at query time." Practice on this date: a relation question is
  answered from the graph together with text retrieval, not instead of it.

## Decision

- One rule, `retrieval.planned_request`: no semantic search → `BASE`; an explicit profile runs
  as declared (grounded recall and the benchmark are unchanged); otherwise, when the planner's
  profile declares the graph, that profile runs with lexical, dense and graph; every other
  question runs `HYBRID` as before.
- `retrieve()` takes the planned `signals`; the search run passes them.
- Without a graph answer, a planned `GRAPH`/`REPO_MAP`/`IMPACT` run reports `HYBRID` when dense
  ran (it reported `BASE`).
- `recall` components list the declared signals and any that ran; the unreported trace names
  the mode the search would run.

## Uncertainty

- Not measured on the live vault: how many relation questions agents send, and whether the
  graph's one-hop neighbours (weight 0.5 in the fusion) improve their answers. A graph that
  fails degrades one signal only, and the change touches only relation questions.

## Files

- `scripts/retrieval.py`
- `scripts/mcp_server.py`
- `tests/test_recall_asks_the_graph_about_relations.py`
- `CHANGELOG.md`
