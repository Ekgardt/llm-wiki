---
type: workflow
title: "Ingestion Workflow"
confidence: high
source_authority: user
description: "New source material is captured into knowledge/inbox or knowledge/raw, then compiled into durable wiki pages."
timestamp: 2026-07-03T05:48:32
---
# Ingestion Workflow

One-sentence summary: New source material is captured into `knowledge/inbox/` or `knowledge/raw/`, then compiled into durable wiki pages.

## Steps
1. **Capture source** into `knowledge/inbox/articles/` (unprocessed staging) or directly into `knowledge/raw/` if the material is immediately trusted. Use Obsidian Web Clipper for web pages; drop PDFs, transcripts, and datasets as files.
2. **Review what is already covered** in `knowledge/notes/`. Read `knowledge/index.md` and any relevant concept/entity/synthesis pages. The goal is to decide whether the new material updates existing pages or warrants new ones.
3. **Create or update durable pages** under `knowledge/notes/`. Every factual claim carries a `Source:` line pointing at the `knowledge/raw/` or `knowledge/inbox/` file. If the source is not yet in `raw/`, mark inferred sections with [[Preliminary Flagging]].
4. **Add a raw-source record** under `knowledge/notes/` if the new material is a named external artifact (article, thread, paper) — this protects the vault from single-point dependence on external URLs.
5. **Update `knowledge/index.md`** by registering the new pages under the correct section.
6. **Append a dated entry to `knowledge/log.md`** describing what was compiled and from which source(s).
7. **Move processed files from `knowledge/inbox/` to `knowledge/raw/`** once their content has been lifted. `inbox/` is staging; `raw/` is immutable source-of-truth. See [[inbox-vs-raw-after-compile]] for the rationale.

## When to invoke this workflow
Run manually via the `/knowledge-compile` skill, or whenever `knowledge/inbox/` accumulates material that has not been reviewed. The `/knowledge-review` skill audits existing `knowledge/notes/` pages without ingesting new ones.

## Source
- [[Karpathy X Thread - April 2026]] (durable wiki record)
- `knowledge/raw/articles/…` (captured originals)

## Related
- [[Retrieval Workflow]]
- [[Review Workflow]]
- [[knowledge/index|Knowledge index]]
- [[index]]
