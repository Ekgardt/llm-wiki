---
type: gap
title: "Knowledge Gaps"
description: "<what this concept IS, even if the page isn't written yet>."
timestamp: 2026-07-03T06:13:53
---
# Knowledge Gaps

This directory tracks **concepts, entities, or topics** that have been mentioned in the vault but do not yet have their own dedicated page. A gap is a placeholder for "not-yet-written knowledge" — it makes absence visible instead of silent.

## When to add a gap

Add a stub page here when:

1. You mention a concept/entity/topic in a wiki page or memory knowledge page that doesn't have its own page yet.
2. You notice during `/lint` that a wikilink target is missing.
3. A user or agent asks a question whose answer is not in the vault but should be.

## Gap page format

```markdown
---
type: gap
title: "<the missing concept>"
description: "<what's missing and why it matters>"
timestamp: <ISO 8601 datetime>
---

# <Concept Name> (gap)

One-sentence summary: <what this concept IS, even if the page isn't written yet>.

## What we know so far
- Bullet points of fragmentary understanding from the pages that mention this concept.

## Why it matters
- One sentence on what having a full page would unlock.

## Mentioned in
- Link to the wiki or memory page that referenced this concept, with context.

## Related
- Link to sibling gaps or related concepts (use real wikilinks to existing pages).
```

## When a gap closes

When a real page is created for the concept:

1. Create the full page in the appropriate directory (`wiki/concepts/`, `memory/knowledge/decisions/`, etc.).
2. Edit the gap page to mark it closed: change `type: gap` to `type: concept` (or the appropriate type) and add a `closed_at:` field, OR delete the gap page if it added no unique content.
3. Ensure the new page **backlinks to the pages that mentioned the gap** — that closes the reference loop.

## Lint support

`lint_memory.py::orphan_gaps` flags gap pages that have no inbound link from outside `wiki/gaps/`. A gap that nobody references is itself an orphan and should either be promoted to a real page or removed.

## Editorial note

This directory is **not** a wiki category in the traditional sense — it is a tracking layer for incomplete knowledge. Pages here should be small (50-150 words) and actionable: each one should answer "what would close this gap?".
