---
title: Obsidian
type: entity
confidence: medium
source_authority: web
---

# Obsidian

One-sentence summary: Obsidian is a Markdown-based note app used here only as an optional human-facing viewer for an LLM-maintained vault.

## Key facts
- Operates over a folder of plain `.md` files, so a vault stays readable without Obsidian.
- Its optional plugin ecosystem can provide alternate rendering of local Markdown outputs (e.g. Marp for slides).
- Native graph view visualizes wikilinks, which makes orphan pages and weak-link clusters visible at a glance — a useful complement to structural lint.

## Optional viewing role
Obsidian provides rendered Markdown, wikilink navigation, and graph visualization with zero server component. Every file remains plain text readable by any editor, and LLM Wiki does not require Obsidian.

## Practical role in this vault
The `.obsidian/` directory (when present — gitignored, local-only) lets Obsidian treat the repo as one vault, so all knowledge zones are browsable from one sidebar. Ingestion remains tool-neutral and file-based.

Obsidian is outside the agent integration boundary. The project does not bundle a Web Clipper, plugin, or write automation for it. Agents read and act through MCP, while lifecycle adapters capture host events. This keeps Obsidian optional and prevents viewer-specific state from becoming a knowledge dependency.

For how Obsidian fits into the broader pipeline, see [[Karpathy LLM Wiki Workflow]] and [[LLM Knowledge Base]].

## Source
- [[Karpathy X Thread - April 2026]] (durable wiki record)
- `knowledge/raw/articles/…` (captured originals)
- `docs/STRUCTURE.md` and `docs/ARCHITECTURE.md` (current optional-viewer and MCP integration boundary)

## Related
- [[LLM Knowledge Base]]
- [[Karpathy LLM Wiki Workflow]]
- [[Andrej Karpathy]]
