---
type: workflow
title: "Review Workflow"
description: "The wiki should be pruned, linked, and quality-checked regularly."
timestamp: 2026-07-03T05:48:32
---
# Review Workflow

One-sentence summary: The wiki should be pruned, linked, and quality-checked regularly.

## Review questions
- Are there orphan pages?
- Are there contradictions?
- Are source references present?
- Are giant pages better split into linked pages?
- Are key concepts/entities missing?

## Monitoring & Metrics
Track a small set of numbers over time to catch drift before it gets expensive:
- **Page count** — total markdown files under `knowledge/notes/`.
- **Orphan ratio** — share of pages with 0 inbound wikilinks (excluding entry points like `index.md`, `log.md`, `Vault Home.md`). Target: < 10%.
- **Average inbound links per page** — healthy wikis trend upward as the graph densifies. Target: ≥ 3.
- **Sourced-page ratio** — share of concept/entity/synthesis pages with a `Source:` line. Target: 100%.
- **Stale pages** — pages not touched in > 90 days that still describe fast-moving topics.

## Review cadence
- **Light lint** — `python scripts/lint_memory.py --scope all` after every 5 new pages ingested, or weekly if active. Seven structural checks (broken wikilinks, orphan pages, orphan daily logs, stale compiled pages, missing backlinks, sparse pages, and opt-in contradictions).
- **Contradiction check** — `/contradict-check` or `python scripts/lint_memory.py --contradictions` before significant commits; installable as a git pre-commit hook with a diff gate that only runs when knowledge pages changed.
- **Full review** — monthly, or whenever the wiki crosses a size threshold (20, 50, 100 pages). Tier-boundary crossings (50 and 300 pages) are natural trigger points because `/knowledge-lookup` changes strategy at those thresholds.
- **Taxonomy review** — quarterly, to decide whether to split broad pages or introduce new top-level sections in [[index]].

## Source
- [[Karpathy X Thread - April 2026]] (durable wiki record)
- `knowledge/raw/articles/…` (captured originals)

## Related
- [[Ingestion Workflow]]
- [[Retrieval Workflow]]
- [[knowledge/index|Knowledge index]]
- [[index]]
