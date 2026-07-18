# v5.0 Roadmap

The v4 components are shipped, but the 2026-07-16 audit found that several of
them are not yet connected through one generation-consistent pipeline. Code and
wikilink graphs are process-local, vectors can be stale, query answering bypasses
retrieval, access telemetry is not cross-process, and context limits are based on
characters rather than tokens.

The canonical implementation plan is:

- [Unified Evidence Retrieval Implementation Plan](superpowers/plans/2026-07-16-unified-evidence-retrieval.md)
- [Task 1 frozen baseline (2026-07-16)](../benchmark/baseline-2026-07-16.md)

## Planned Critical Path

1. Close the remaining Stage 1 and Stage 2 integration gaps.
2. Freeze EN/RU/ZH quality, token, latency, and Graphify comparison baselines.
3. Introduce coherent corpus generations and a truthful retrieval contract.
4. Build the Adaptive Context Compiler and evidence-grounded QA.
5. Persist code, knowledge, and project relationships in a derived Evidence Graph.
6. Add full-rebuild-equivalent incremental indexing and graph-backed impact analysis.
7. Publish reproducible quality, token, freshness, and failure evidence.

## Preserved v4 Foundations

- Tree-sitter queries and lazy optional grammars for 12 languages.
- Python/Jedi semantic resolution and canonical qualified symbols.
- FTS5, optional vectors, RRF, reranking, and L0/L1/L2 tiers.
- Typed Markdown knowledge, project journals, checkpoints, and supersession.
- Recoverable Markdown transactions, queue leases, archives, claims, and evidence.
- Twelve task-shaped MCP tools with one response envelope.

## Deferred Until The Pipeline Is Proven

- Full temporal time-travel graph queries.
- Cross-repository and cross-service topology.
- Visual graph explorer.
- Broad document/media ingestion.
- Additional language expansion.
- HTTP MCP and package-wide CLI migration.
- Learned retrieval policies and default LLM-generated graph/context artifacts.

The derived graph never replaces Markdown, Git, or project journals as sources of
truth. No deferred feature may block the critical path above.
