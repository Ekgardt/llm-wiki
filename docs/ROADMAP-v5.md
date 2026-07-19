# v5.0 Roadmap

The 2026-07-16 audit defined a 30-task generation-consistent retrieval program.
Tasks 1-29 are integrated on the current development branch: immutable generations,
truthful retrieval, token-aware context, grounded QA, persistent evidence/code/
knowledge/project graphs, incremental reuse, impact analysis, security gates, and
scale/comparative smoke contracts now exist. Task 30 is the documentation and
migration sync represented by this change.

The canonical implementation plan is:

- [Unified Evidence Retrieval Implementation Plan](superpowers/plans/2026-07-16-unified-evidence-retrieval.md)
- [Task 1 frozen baseline (2026-07-16)](../benchmark/baseline-2026-07-16.md)

## Integrated Critical Path

1. Completed: coherent corpus generations and a truthful retrieval contract.
2. Completed: Adaptive Context Compiler and evidence-grounded QA contracts.
3. Completed: derived code, knowledge, and project Evidence Graph extraction.
4. Completed: incremental reuse, graph-backed impact analysis, and store-first code tools.
5. Completed: adversarial, scale, crash, and comparative smoke harness contracts.
6. Evidence pending: complete raw EN/RU/ZH model-selection runs and a selected default.
7. Evidence pending: real paired Graphify execution, confidence intervals, and public comparison claims.
8. Evidence pending: installed-vault migration proof sufficient to remove legacy readers/caches.

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

## Release Evidence Status

- No pyproject version bump is part of Task 30.
- Existing README test counts remain unchanged until final integration collection.
- The deterministic comparative smoke does not execute Graphify and cannot support
  Graphify parity, quality, or token-ratio claims.
- The model matrix has no selected embedding or reranker default; raw selection
  evidence is pending.
- Generation correctness and recovery are covered by integrated automated tests, but
  installed-vault migration and legacy-cache-removal evidence is pending.
