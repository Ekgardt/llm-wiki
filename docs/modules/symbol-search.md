# Ranked symbol search over the whole active generation (issue #24, B2)

`scripts/symbol_search.py` and the tests that exercise them most.

Parity with codebase-memory-mcp's `search_graph` name search: a name pattern answers qualified names with in/out degree, an exact `total` and a `has_more` flag, ranked exact > prefix > substring > in-degree. The search is one SQL statement over the generation's `node` table (`EvidenceGraph.search_nodes`); no artifact is added and no generation format changes. Not done, by design: BM25 identifier splitting and semantic search over symbols — both need a symbol-level artifact the generation does not hold. Research: `docs/research/2026-09-10-a-query-surface-that-answers-the-whole-graph.md`.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
