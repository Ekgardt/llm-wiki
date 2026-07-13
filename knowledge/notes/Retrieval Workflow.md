---
type: workflow
title: "Retrieval Workflow"
confidence: high
source_authority: user
description: "Answers should come from the compiled wiki first, then from raw material only when needed, with the exact strategy picked by vault size."
timestamp: 2026-07-03T05:48:32
---
# Retrieval Workflow

One-sentence summary: Answers should come from the compiled wiki first, then from raw material only when needed, with the exact strategy picked by vault size.

## Retrieval priority (baseline)
1. **`knowledge/index.md`** — read it first; it is the navigation map over every durable page.
2. **Relevant pages in `knowledge/notes/`** — concepts, entities, syntheses, comparisons, connections, Q&A.
3. **`knowledge/daily/`** — recent episodic capture when notes are stale or incomplete.
4. **`knowledge/raw/` or `knowledge/inbox/`** — fall back only when the wiki is missing or stale. If a gap surfaces, recommend an ingestion pass rather than answering from raw directly.
5. **Local search** — SQLite FTS5 BM25 is the base index at `$LLM_WIKI_STATE_ROOT/cache/index.sqlite`; vectors/LanceDB, graph neighbors, and reranking are optional.

## Tiered strategy (via `/knowledge-lookup`)
The `/knowledge-lookup` skill now picks the retrieval strategy based on curated wiki page count, computed by `python scripts/lookup_mode.py`:
- **DIRECT (<50 pages)** — read `knowledge/index.md` + target pages. Current vault sits here.
- **BASE (50–300)** — wiki-first, then use local SQLite FTS5 BM25 when direct navigation is unconvincing.
- **HYBRID (>300)** — use BM25 plus optional vectors/LanceDB, graph neighbors, and reranking; the index remains a navigation surface.

## Response discipline
Regardless of tier: prefer synthesis over quotation, explicitly state uncertainty, cite the pages that drove the answer, and offer `/knowledge-qa-file-back` when the question is non-obvious and likely to recur.

## Source
- [[Karpathy X Thread - April 2026]] (durable wiki record)
- `knowledge/raw/articles/…` (captured originals)

## Related
- [[Ingestion Workflow]]
- [[Review Workflow]]
- [[knowledge/index|Knowledge index]]
- [[index]]
