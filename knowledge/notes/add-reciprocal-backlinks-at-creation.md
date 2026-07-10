---
type: pattern
title: "Add Reciprocal Backlinks at Creation"
description: "When creating a new synthesis, concept, or decision page that references existing pages, add all reciprocal backlinks to the related pages in the same editing pass — never defer them to a future clean"
timestamp: 2026-07-03T05:41:37
confidence: medium
source_authority: user
---
# Add Reciprocal Backlinks at Creation

One-sentence summary: When creating a new synthesis, concept, or decision page that references existing pages, add all reciprocal backlinks to the related pages in the same editing pass — never defer them to a future cleanup round.

## Lesson

When you write `[[Memory Subsystem Action Plan]]` in a new page, the action plan page has no idea the new page exists. Lint will eventually surface the missing backlink, but by then the context is cold and the connection rationale is forgotten. Adding the reciprocal link *at creation time* is nearly free and closes the loop permanently.

**Apply to:** any new page in `knowledge/notes/` or `knowledge/notes/` that uses a wikilink to an existing page.

**What to add on the target page:** a short `## Related` or inline link with a one-clause rationale for the connection (e.g., "— the plan that motivated this pattern"). A bare link with no rationale is acceptable but a brief phrase is better.

**Scope:** reciprocals are required when both pages are durable content (concepts, patterns, decisions, syntheses, entities, connections). Skip for append-only logs (`log.md`, daily files) and ephemeral state pages (`state.md`).

## When NOT to apply
- The target is an editorial/metadata page (`index.md`, `log.md`, `state.md`) — these are exempt from backlink obligations.
- The relationship is incidental (one page happens to mention another in passing with no real conceptual tie). Don't litter pages with shallow backlinks.

## Evidence
- `knowledge/daily/2026-04-19.md` [03:00:46] — lesson recorded after new [[Global Multi-Project Migration Plan]] page was created with 4 backlinks but no reciprocals; all five target pages had to be updated in a follow-up pass.

## Related
- [[knowledge/notes/audit-current-vs-intended]] — complementary: once you have reciprocal backlinks, audits catch drift between intent and reality
- [[knowledge/notes/provenance-rule-6]] — underlying rule: make relationships explicit, not implicit
