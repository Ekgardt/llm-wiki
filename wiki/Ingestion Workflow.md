---
type: workflow
title: "Ingestion Workflow"
description: "New source material is captured into `inbox/` or `raw/`, then compiled into durable wiki pages."
timestamp: 2026-07-03T05:48:32
---
# Ingestion Workflow

One-sentence summary: New source material is captured into `inbox/` or `raw/`, then compiled into durable wiki pages.

## Steps
1. **Capture source** into `inbox/articles/` (unprocessed staging) or directly into `raw/` if the material is immediately trusted. Use Obsidian Web Clipper for web pages; drop PDFs, transcripts, and datasets as files.
2. **Review what is already covered** in `wiki/`. Read `wiki/index.md` and any relevant concept/entity/synthesis pages. The goal is to decide whether the new material updates existing pages or warrants new ones.
3. **Create or update durable pages** under `wiki/concepts/`, `wiki/entities/`, `wiki/syntheses/`, `wiki/comparisons/`, or `wiki/connections/`. Every factual claim carries a `Source:` line pointing at the `raw/` or `inbox/` file. If the source is not yet in `raw/`, mark inferred sections with [[Preliminary Flagging]].
4. **Add a raw-source record** under `wiki/raw-sources/` if the new material is a named external artifact (article, thread, paper) — this protects the vault from single-point dependence on external URLs.
5. **Update `wiki/index.md`** by registering the new pages under the correct section.
6. **Append a dated entry to `wiki/log.md`** describing what was compiled and from which source(s).
7. **Move processed files from `inbox/` to `raw/`** once their content has been lifted. `inbox/` is staging; `raw/` is immutable source-of-truth. See [[memory/knowledge/qa/inbox-vs-raw-after-compile]] for the rationale.

## When to invoke this workflow
Run manually via the `/knowledge-compile` skill, or whenever `inbox/` accumulates material that has not been reviewed. The `/knowledge-review` skill audits existing `wiki/` pages without ingesting new ones.

## Source
- [[Karpathy X Thread - April 2026]] (durable wiki record)
- `raw/articles/Thread by @karpathy.md` (captured original)

## Related
- [[Retrieval Workflow]]
- [[Review Workflow]]
- [[Vault Home]]
- [[index]]
