---
type: skill
name: bridge-promote-insight
argument-hint: "[page or topic]"
description: Promote a durable insight from session memory into the external research wiki, or link the two layers together.
disable-model-invocation: true
allowed-tools: Read Glob Grep LS Edit Write
title: "SKILL"
timestamp: 2026-07-03T05:41:37
---
Use this when an insight discovered during coding/work sessions deserves to become part of the broader research wiki.

Procedure:
1. Read the relevant page(s) from `memory/knowledge/`.
2. Decide whether the insight belongs in:
   - `wiki/concepts/`
   - `wiki/syntheses/`
   - `wiki/comparisons/`
   - `wiki/connections/` (cross-concept edge observations)
   - or only as a cross-link from existing pages.
3. Create or update the target wiki page. Required elements on the new wiki page:
   - standard wiki frontmatter + `One-sentence summary:` line
   - a `## Source` block that names the memory origin page explicitly, e.g.:
     > Origin: session memory — `memory/knowledge/<category>/<slug>` (distilled from `memory/daily/YYYY-MM-DD.md` [HH:MM:SS]).
   - a closing paragraph stating *"This page is a **promotion from session memory**, not a compilation of external source material."* — tells future readers why there is no `raw/` citation.
4. Add a `## Promoted to wiki` section to the **memory origin page** (append, don't replace earlier content). Format:
   ```
   ## Promoted to wiki
   Promoted YYYY-MM-DD to [[Wiki Page Title]] (`wiki/<section>/`). This memory page remains as <reason — usually "the dated decision record" or "the fuller origin story">; the wiki page is the concise public-facing <concept/convention/definition>.
   ```
   Then add the wiki page as the first entry in the memory page's `## Related` section.
5. Confirm the links are reciprocal — the wiki page's `## Related` should contain a `[[memory/knowledge/<category>/<slug>]]` wikilink back. Both pages must mention each other; a one-way promotion marker is a bug.
6. Register the new wiki page in `wiki/index.md` under the correct section (Concepts / Syntheses / Comparisons / Connections) and append a dated entry to `wiki/log.md` describing the promotion and citing the memory origin.

Return:
- path of the created or updated wiki page
- path of the memory origin page now carrying the `## Promoted to wiki` marker
- one-line rationale for the target section choice

Anti-patterns:
- Don't delete the memory page after promoting — its date-stamped record stays as provenance.
- Don't paste the memory content verbatim into wiki — rephrase in third-person for a reader without session context.
- Don't skip step 4; without the reciprocal marker `lint_memory.py` will flag the memory page as having lost a connection, and future promotions won't find the precedent.
