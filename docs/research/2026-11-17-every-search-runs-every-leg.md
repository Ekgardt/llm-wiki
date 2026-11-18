# Every search runs every leg

Date: 2026-11-17. Supersedes the planning rule of
`docs/research/2026-09-25-recall-asks-the-graph-about-relations.md` (a relation-worded
question adds the graph, every other question runs `HYBRID`).

## Facts (checked in code before the change)

- `retrieval.analyze_query` read the profile from word lists in three languages:
  `_TEMPORAL_RE`, `_GRAPH_RE`, `_REPO_MAP_RE`, `_IMPACT_RE`, `_GLOBAL_RE`, `_QUESTION_RE` and
  `_CROSS_LANG_RE`. `planned_request` turned a graph-declaring recommendation into that
  profile plus dense, and every other recommendation into `HYBRID`.
- `retrieve()` called with no profile ran the recommendation as given, so a query read as
  `TEMPORAL` or `EXACT` ran lexical only.
- `provenance.CODE_SHAPED_INTENTS` also counted the words "depends on", "impact" and
  "repo map" as a code question, which switched the curated-pages prior off.
- A wrong reading was never recovered downstream. Recorded on this vault
  (`tests/test_semantic_leg_reaches_the_answer.py`): a topic question holding the word
  "after" was routed to `TEMPORAL`, lost the dense leg, and the grounded answer reported
  insufficient evidence although the vault held the decision page.
- The owner rejected word lists as a class: a list covers the phrasings someone thought
  of and misses the rest, in every language.

## Sources (abstracts re-read 2026-11-17)

1. C. Wu, Y. Lin, "Evidence-Unit Fairness and the Limits of Query-Adaptive Sparse-Dense
   Fusion in Financial Document Retrieval", arXiv 2608.00183: a per-query oracle shows 21.8%
   headroom over a fixed sparse-dense blend, yet none of three lightweight adaptive routers
   (a score-confidence heuristic, a random forest over query features, a ridge regressor
   over query embeddings) gave a statistically reliable improvement over the fixed blend.
2. Z. Hussain, K. Nielbo, "The Coverage Illusion: From Pre-retrieval Routing Failure to
   Post-retrieval Cascades in a Production RAG System", arXiv 2605.27220: whether a query
   needs more than plain retrieval shows only after the index is searched; four machine
   learning paradigms could not predict it before retrieval. Their answer decides after
   retrieval, not before it.
3. Y. Guo et al., "R²Adapter: A Routing and Rewriting Adapter for Efficient Hybrid RAG",
   arXiv 2609.02894: a router that sends only graph-benefiting queries to graph RAG cuts
   graph usage by up to 59% at comparable accuracy. The router's gain is cost; it does not
   raise accuracy over using the graph.
4. Microsoft GraphRAG, "Local Search",
   https://microsoft.github.io/graphrag/query/local_search/: "The local search method
   combines structured data from the knowledge graph with unstructured data from the input
   documents" -- the graph is used together with text retrieval, not instead of it.

What these do not say: none measures this vault, and none compares a word-list router with
always-on fusion on personal memory. The choice rests on sources 1 and 2 (a query-time
router does not reliably beat a fixed fusion, and the need is visible only after
retrieval), on source 3 (the router's value is cost), and on the routing error recorded
on this vault below.

## Alternatives considered

1. Keep the word router and add phrasings. Rejected: it is the class of error the vault
   already recorded, and sources 1 and 2 find no reliable gain from routing before retrieval.
2. Never use the graph unless a caller names a graph profile. Rejected: the agent asking a
   relation question in words would never get the graph (the C-22 defect of 2026-09-25).
3. A learned or LLM router. Rejected: source 2 found learned routers could not predict the
   need before retrieval, source 3 finds the value is cost, and it adds a model call before
   every search.
4. Always run every leg and let fusion and the cross-encoder order the pool. Chosen.

## Decision

- `analyze_query` reads only the query's shape: a quoted phrase, a name, or a query made of
  names only (`identifier_lookup`). No word of a sentence is read.
- With no profile named, a semantic search runs `retrieval.PLANNED_PROFILE` (`GLOBAL`):
  lexical, dense and the evidence graph, then the cross-encoder. `retrieve()` with no profile
  plans the same. An explicit profile still runs as declared; `semantic=False` is still
  lexical only.
- A lookup made only of names keeps its exact-match bypass of the cross-encoder; that is
  read from the shape, not from a profile.
- The curated-pages prior is switched off only by a name in the query
  (`CODE_SHAPED_INTENTS = {"exact_identifier"}`).
- Without a graph answer the run reports `HYBRID`, as before, with `graph_unavailable` as
  its fallback reason. The production builder always writes `evidence.sqlite3` into a
  generation (`scripts/evidence_graph_builder.py`), so that reason means the graph really is
  missing; a generation without it is a test fixture or a damaged build.
- A caller that switches the graph off (`graph=False`) is planned `HYBRID`, not a graph it
  disabled: otherwise every such search would report `graph_disabled`.
- With no generation, the Markdown fallback's graph is the wikilink-neighbour boost, so its
  rows may say `lexical` and `graph`; they never say `dense`.
- Graph rows obey the caller's hard filters exactly as a lexical hit does (added 2026-11-18,
  after review). Before, a graph-only neighbour from another project, outside the
  `since`/`as_of` window, or superseded could enter the candidates; it was rare while only
  relation-worded questions asked the graph, and every search asks it now. On a generation
  the neighbour paths are checked against the same `chunks` filters the lexical leg uses
  (`search_memory.generation_eligible_paths`); on the Markdown fallback each neighbour page
  is read and filtered like a lexical page (`search_memory.markdown_page_row`), and the
  wikilink walk is bounded by the search deadline.
- Text overlap orders graph rows instead of admitting them (2026-11-18). The old rule
  dropped a neighbour sharing no word with the query unless the profile was a declared
  graph profile; with every search planned `GLOBAL`, that dropped exactly the neighbours a
  relation question asks for. A neighbour with no shared word now ranks after every graph
  row that shares one, and fusion still ranks a graph-only candidate below text hits.
- Dates are no longer read from words. A dated window is the caller's explicit
  `since`/`until` filter; the grounded answer's own dated leg is unchanged.

## Trade-offs and uncertainty

- Not measured on a real vault: no stand in this worktree has a graph with edges, and the
  LongMemEval vault graph has none (`docs/research/2026-09-22-the-walk-has-nothing-to-walk.md`),
  so there the graph leg changes nothing but its own latency. Whether the graph helps or hurts
  ranking on the owner's vault is open; the fusion weight is 0.5 against 2.0 lexical and
  1.0 dense, so a graph-only candidate ranks low and the graph mostly lifts candidates the
  other legs already found.
- The graph leg's latency on a vault with edges is not measured.
- A code question written only in words ("show the project structure") now keeps the
  curated-pages prior, so a source file ranks at the `doc` weight there. A question that
  names a file or a symbol is unaffected.
- A question that only words a time ("decisions since 2025") no longer becomes lexical
  only; it runs every leg. It is not filtered to those dates unless the caller passes the
  dates.

## Files

- `scripts/retrieval.py`, `scripts/provenance.py`, `scripts/query_memory.py`,
  `scripts/mcp_server.py`, `scripts/lookup_mode.py`
- `benchmark/longmemeval_vault.py`, `benchmark/consolidation_vault.py`
- `tests/test_every_search_runs_every_leg.py` (replaces
  `tests/test_recall_asks_the_graph_about_relations.py`) and the analyzer tests that pinned
  word routes
