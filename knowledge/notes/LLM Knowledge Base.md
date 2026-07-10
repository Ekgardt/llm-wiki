---
title: LLM Knowledge Base
type: concept
confidence: medium
source_authority: web
---

# LLM Knowledge Base

One-sentence summary: A personal, LLM-maintained markdown wiki compiled from raw source documents, where the LLM (not the human) writes and curates the durable knowledge layer.

## Key facts
- Raw source documents (articles, papers, repos, datasets, images) are indexed into a `knowledge/raw/` directory and treated as immutable evidence.
- An LLM incrementally "compiles" a wiki of `.md` files: summaries, concept articles, backlinks, and categorical structure.
- The wiki itself is the LLM's domain; the human rarely writes or edits it directly.
- At ~100 articles / ~400K words, plain LLM reading over auto-maintained index + summary files substitutes effectively for dedicated RAG.
- Outputs of queries (markdown notes, Marp slides, matplotlib images) can be "filed back" into the wiki so explorations compound.
- Periodic LLM "linting" / health checks find inconsistencies, impute missing data via web search, and suggest new article candidates.
- A long-term extension is synthetic data generation + finetuning so the model internalizes the knowledge in weights rather than context.

## Workflow stages (per [[Andrej Karpathy]])
1. **Data ingest** — drop sources into `knowledge/raw/`; LLM compiles into wiki `.md` files.
2. **IDE** — Obsidian as frontend to view raw, compiled wiki, and derived visualizations.
3. **Q&A** — agent answers complex questions by reading the wiki directly.
4. **Output** — responses rendered as markdown, slides, or images, viewable in Obsidian.
5. **Linting** — health-check passes to maintain integrity.
6. **Extra tools** — e.g. a small search engine over the wiki, used directly or handed to the LLM as a CLI tool.

## Open questions
- At what scale does direct-read break down and force real RAG?
- How should synthetic-data finetuning be evaluated against the always-fresh read-the-wiki approach?

## Source
- [[Karpathy X Thread - April 2026]] (durable wiki record)
- `knowledge/raw/articles/Thread by @karpathy.md` (captured original)

## Related
- [[Andrej Karpathy]]
- [[Obsidian]]
- [[Karpathy LLM Wiki Workflow]]
- [[Ingestion Workflow]]
- [[Retrieval Workflow]]
- [[Review Workflow]]
- [[Wiki vs Memory Compiler vs Fusion]] — comparison that evaluates this concept against alternative persistence strategies.
- [[Product Requirements in the AI Era]] — a 2026 research synthesis compiled into the wiki; demonstrates the ingest→wiki flow.
