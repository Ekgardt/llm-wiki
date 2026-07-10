---
title: Karpathy LLM Wiki Workflow
type: concept
confidence: medium
source_authority: web
---

# Karpathy LLM Wiki Workflow

One-sentence summary: End-to-end pattern from [[Andrej Karpathy]]'s April 2026 thread for turning raw source material into an LLM-maintained markdown wiki viewed through [[Obsidian]].

## Pipeline
1. **Ingest** — drop articles, papers, repos, datasets, and images into `knowledge/raw/`. Use Obsidian Web Clipper for web pages; a hotkey pulls related images locally so the LLM can reference them.
2. **Compile** — an LLM incrementally builds a `.md` wiki: summaries, concept/entity articles, categorization, and backlinks. Index and summary files are auto-maintained.
3. **View** — Obsidian is the frontend for raw data, compiled wiki, and derived visualizations. The human rarely edits the wiki directly.
4. **Query** — agent answers complex questions by reading the wiki; at ~100 articles / ~400K words this is sufficient without dedicated RAG.
5. **Output** — responses rendered as markdown, Marp slides, or matplotlib images; often filed back into the wiki so explorations accumulate.
6. **Lint** — periodic LLM health checks flag inconsistencies, impute gaps via web search, and propose new article candidates.
7. **Tooling** — supplemental CLIs (e.g. a small search engine over the wiki) handed to the LLM for larger queries.
8. **Future** — synthetic data generation + finetuning so the LLM internalizes the corpus in weights.

## Why it matters
- Flips the split of human/LLM labor: humans collect sources and pose questions; the LLM owns curation.
- Suggests that careful directory + summary discipline can defer or replace retrieval infrastructure at modest scale.
- Outputs compound back into inputs, producing a self-reinforcing knowledge loop.

## Open questions
- Concrete scale ceiling before direct-read fails.
- Best practices for linting prompts and cadence.
- When synthetic-data finetuning pays off vs. staying context-native.

## Source
- [[Karpathy X Thread - April 2026]] (durable wiki record)
- `knowledge/raw/articles/Thread by @karpathy.md` (captured original)

## Related
- [[LLM Knowledge Base]]
- [[Andrej Karpathy]]
- [[Obsidian]]
- [[Ingestion Workflow]]
- [[Retrieval Workflow]]
- [[Review Workflow]]
