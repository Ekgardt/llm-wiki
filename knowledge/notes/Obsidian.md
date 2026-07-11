---
title: Obsidian
type: entity
confidence: medium
source_authority: web
---

# Obsidian

One-sentence summary: Markdown-based note app used as the human-facing viewer for an LLM-maintained vault.

## Key facts
- Operates over a folder of plain `.md` files, so a vault stays readable without Obsidian.
- **Obsidian Web Clipper** turns web pages into `.md` suitable for `knowledge/raw/` ingestion.
- Plugin ecosystem allows alternate rendering of LLM outputs (e.g. Marp for slides).
- Native graph view visualizes wikilinks, which makes orphan pages and weak-link clusters visible at a glance — a useful complement to structural lint.

## Why Obsidian is the frontend here, not VS Code or a web UI
The vault's operating pattern assumes the human reads markdown rendered, not raw. Obsidian gives rendered view, wikilink navigation, and graph visualization out of the box, with zero server component. Every file stays a plain text file readable by any editor — so the vault is not locked to Obsidian even though Obsidian is the canonical viewer. If Obsidian disappeared, the same folder would open in any markdown tool without loss.

## Practical role in this vault
The `.obsidian/` directory (when present — gitignored, local-only) configures Obsidian to treat the repo as one vault — so `knowledge/raw/`, `knowledge/inbox/`, `knowledge/notes/`, `knowledge/projects/`, and `knowledge/daily/` are all browsable from the same sidebar. Obsidian Web Clipper is the recommended path for capturing an article into `knowledge/inbox/articles/` before it is compiled into `knowledge/notes/` and eventually moved to `knowledge/raw/`.

For how Obsidian fits into the broader pipeline, see [[Karpathy LLM Wiki Workflow]] and [[LLM Knowledge Base]].

## Source
- [[Karpathy X Thread - April 2026]] (durable wiki record)
- `knowledge/raw/articles/…` (captured originals)

## Related
- [[LLM Knowledge Base]]
- [[Karpathy LLM Wiki Workflow]]
- [[Andrej Karpathy]]
