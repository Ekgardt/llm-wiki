---
title: Karpathy X Thread - April 2026
type: raw-source
author: Andrej Karpathy
published: 2026-04-02
captured: 2026-04-11
---

# Karpathy X Thread — April 2026

One-sentence summary: Durable wiki-side pointer to the April 2026 X thread by [[Andrej Karpathy]] introducing the "LLM knowledge base" pattern — exists so the vault is not solely dependent on the external post.

## Provenance
- **Author:** @karpathy (Andrej Karpathy)
- **Published:** 2026-04-02
- **Captured into inbox:** 2026-04-11
- **Original URL:** https://x.com/karpathy/status/2039805659525644595
- **Local file (immutable):** `raw/articles/Thread by @karpathy.md`

## What the thread covers
Karpathy describes using an LLM to maintain a personal markdown knowledge base:
- **Data ingest** — source documents go into `raw/`; an LLM incrementally compiles them into a `.md` wiki with summaries, backlinks, and concept articles. Obsidian Web Clipper is used to convert web pages; a hotkey fetches related images locally.
- **IDE** — [[Obsidian]] is the frontend for raw data, compiled wiki, and derived visualizations. The LLM writes the wiki; the human rarely edits directly.
- **Q&A** — at ~100 articles / ~400K words, direct LLM reading over auto-maintained index and summary files replaces dedicated RAG.
- **Output** — answers are rendered as markdown, Marp slides, or matplotlib images, and often filed back into the wiki so explorations compound.
- **Linting** — periodic LLM health checks flag inconsistencies, impute gaps via web search, and propose new article candidates.
- **Extra tools** — supplemental CLIs (e.g. a small wiki search engine) handed to the LLM for larger queries.
- **Further explorations** — synthetic data generation + finetuning so the LLM knows the corpus in weights.

See [[Karpathy LLM Wiki Workflow]] for the structured pipeline and [[LLM Knowledge Base]] for the general concept.

## Derived pages
Pages in this wiki that cite this thread as their source:
- [[LLM Knowledge Base]]
- [[Karpathy LLM Wiki Workflow]]
- [[Andrej Karpathy]]
- [[Obsidian]]
- [[Ingestion Workflow]]
- [[Retrieval Workflow]]
- [[Review Workflow]]

## Source
- `raw/articles/Thread by @karpathy.md` (local captured copy; immutable)
- https://x.com/karpathy/status/2039805659525644595 (original)

## Related
- [[Andrej Karpathy]]
- [[Karpathy LLM Wiki Workflow]]
- [[LLM Knowledge Base]]
