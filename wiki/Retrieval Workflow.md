---
type: workflow
title: "Retrieval Workflow"
description: "Answers should come from the compiled wiki first, then from raw material only when needed, with the exact strategy picked by vault size."
timestamp: 2026-07-03T05:48:32
---
# Retrieval Workflow

One-sentence summary: Answers should come from the compiled wiki first, then from raw material only when needed, with the exact strategy picked by vault size.

## Retrieval priority (baseline)
1. **`wiki/index.md`** — read it first; it is the navigation map over every durable page.
2. **Relevant pages in `wiki/`** — concepts, entities, syntheses, comparisons, connections, Q&A.
3. **`memory/knowledge/`** — consult when the question is about internal workflow, conventions, or how-we-work-here lore rather than external knowledge.
4. **`raw/` or `inbox/`** — fall back here only when the wiki is missing or stale. If a gap surfaces, recommend an ingestion pass rather than answering from raw directly.
5. **QMD search** — optional local hybrid lex+vec search over the whole vault, indexed at `$LLM_WIKI_STATE_ROOT/qmd/index.sqlite` (default sibling-of-vault path; see `scripts/lookup_mode.py::qmd_status` for the env-derived resolution order).

## Tiered strategy (via `/knowledge-lookup`)
The `/knowledge-lookup` skill now picks the retrieval strategy based on curated wiki page count, computed by `python scripts/lookup_mode.py`:
- **DIRECT (<50 pages)** — read `wiki/index.md` + target pages; skip QMD entirely. Current vault sits here.
- **HYBRID (50–300)** — wiki-first, fall back to `qmd search` and `qmd query` only when the direct read is unconvincing.
- **QMD (>300)** — `qmd query` primary, index becomes navigation rather than retrieval surface.

## Response discipline
Regardless of tier: prefer synthesis over quotation, explicitly state uncertainty, cite the pages that drove the answer, and offer `/knowledge-qa-file-back` when the question is non-obvious and likely to recur.

## Source
- [[Karpathy X Thread - April 2026]] (durable wiki record)
- `raw/articles/Thread by @karpathy.md` (captured original)

## Related
- [[Ingestion Workflow]]
- [[Review Workflow]]
- [[Vault Home]]
- [[index]]
