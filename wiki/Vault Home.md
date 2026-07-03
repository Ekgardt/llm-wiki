# Vault Home

One-sentence summary: Human-readable front door to the compiled wiki — start here to orient, then jump to the index or a workflow.

## What this vault is
A persistent markdown knowledge base that compiles durable notes from `raw/` and `inbox/` into linked wiki pages. The goal is a small, well-linked layer that answers questions faster than re-reading sources.

## How to use it
- Looking for a topic? → [[index]] has the full page list.
- Adding new material? → follow [[Ingestion Workflow]].
- Answering a question? → follow [[Retrieval Workflow]].
- Auditing wiki health? → follow [[Review Workflow]].

## Major domains
Current coverage is narrow (one source ingested so far). Active domains:
- **LLM knowledge bases** — see [[LLM Knowledge Base]] and [[Karpathy LLM Wiki Workflow]]
- **Tools** — see [[Obsidian]]
- **People** — see [[Andrej Karpathy]]

New domains will be added as sources are ingested.

## Conventions at a glance
- `raw/` is immutable; `inbox/` is staging; `wiki/` is the compiled layer.
- Factual claims carry a `Source:` line.
- Stable concepts are linked with `[[wikilinks]]`.
- History is tracked in [[log]], not silently overwritten.

## Related
- [[index]]
- [[Ingestion Workflow]]
- [[Retrieval Workflow]]
- [[Review Workflow]]

## Editorial note
This page is vault metadata — a human-readable front door, not content derived from a raw source. Maintained editorially alongside [[index]] and [[log]].
