---
title: Andrej Karpathy
type: entity
confidence: medium
source_authority: web
---

# Andrej Karpathy

One-sentence summary: AI researcher and educator; source author of the April 2026 X thread that this vault's operating pattern is modeled on.

## Context
- Known for deep-learning research and education (ex-OpenAI, ex-Tesla AI, Stanford CS231n).
- In April 2026 published an X thread describing how he uses an LLM to maintain a personal markdown knowledge base, naming [[Obsidian]] as the frontend.
- Details of the pipeline he describes live on [[Karpathy LLM Wiki Workflow]]; the general pattern lives on [[LLM Knowledge Base]]. This page is intentionally kept to the person and his role as source.

## Why his framing matters for this vault
The April 2026 thread is unusual in that it treats the knowledge base as software — a compiled artifact produced by a compiler (the LLM) from source inputs (`knowledge/raw/`). Most prior writing on "personal knowledge management" treats the note system as a second brain the human authors. Karpathy's inversion — the human curates sources, the LLM writes the wiki — is what this vault operationalizes.

The thread is also the canonical justification for preferring markdown + wikilinks over a vector database at modest corpus size. That claim underpins the three-tier retrieval strategy in `/knowledge-lookup`: DIRECT below ~50 pages, HYBRID between 50–300, QMD only past ~300. All three thresholds are conservative interpretations of Karpathy's "~100 articles / ~400K words" direct-read claim.

## Role in this vault
This page is the entity record; the thread it references is preserved at [[Karpathy X Thread - April 2026]] so the vault doesn't depend on the external URL staying alive.

## Source
- [[Karpathy X Thread - April 2026]] (durable wiki record)
- `knowledge/raw/articles/Thread by @karpathy.md` (captured original, 2026-04-02)

## Related
- [[Karpathy LLM Wiki Workflow]]
- [[LLM Knowledge Base]]
- [[Obsidian]]
