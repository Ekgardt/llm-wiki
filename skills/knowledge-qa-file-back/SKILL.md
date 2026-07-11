---
type: skill
name: knowledge-qa-file-back
argument-hint: "[optional slug hint]"
description: Promote a just-answered question into a durable Q&A page under
 `knowledge/notes/` — external-domain questions about material in `knowledge/raw/`
 or internal "how we work here" questions.
disable-model-invocation: true
allowed-tools: Read Glob Grep LS Edit Write Bash(python scripts/rebuild_memory_index.py)
title: "Knowledge QA File Back"
timestamp: 2026-07-03T05:41:37
---
Use this immediately after answering a non-obvious question that is likely to be asked again. The answer then stops being ephemeral chat and becomes a searchable page.

## When to use
Good candidates:
- The user (or you) had to reason from multiple sources to reach the answer.
- The answer settles an ambiguity that CLAUDE.md / AGENTS.md / operating-model.md do not directly cover.
- You can imagine the same question resurfacing in a future session.

Do **not** file back when:
- The answer is already covered by an existing page (update that page instead, or just cross-link).
- The question was about ephemeral session state ("what did we just change?").
- The answer was pure speculation not validated by use.

## Routing decision — which tree?
All knowledge pages live **flat** under `knowledge/notes/<slug>.md`. The `type: qa` frontmatter distinguishes Q&A pages from other types.

1. **Would the answer be useful to someone who had never seen this repo's session history?**
   - *Yes* → it's external-domain knowledge.
   - *No* → it's project lore. Still lives in `knowledge/notes/`, just with `type: qa`.

2. **Does the answer cite `knowledge/raw/` material, public sources, or named authors?**
   - *Yes* → strengthens the external-domain case.
   - *No, only internal files / conventions / hooks / scripts* → project lore, still flat under `knowledge/notes/`.

## Procedure

1. **Pick a slug** — kebab-case, phrased as the question's key phrase (not the answer).
   - Good: `inbox-vs-raw-after-compile`, `when-to-rebuild-search-index`
   - Bad: `question-about-inbox`, `answer-1`

2. **Write the page** at `knowledge/notes/<slug>.md` — follow wiki conventions (frontmatter with `type: qa`, `One-sentence summary:`, `## Question`, `## Answer`, `## Evidence` pointing to the daily log + timestamp, `## Related`).

3. **Register and log**:
   - Add the new page under the existing top-level `## Q&A` section in `knowledge/index.md`, then append a dated entry to `knowledge/log.md`.
   - Or run `python scripts/rebuild_memory_index.py` if the index is auto-generated.

4. **Cross-link**:
   - Add a wikilink to the new Q&A page from the most closely related concept/workflow page.

## Anti-patterns
- Don't invent a Q the user did not actually ask. File-back records real Q&A, not imagined FAQ material.
- Don't duplicate the answer across both trees. Pick one home; cross-link from the other.
- Don't skip step 4 — an unregistered Q&A page will be flagged as `orphan_pages` by `lint_memory.py`.

## Return
- The path of the created page.
- The routing decision and why (one line).
- Any follow-ups (e.g. "consider promoting via bridge-promote-insight after N more related questions").
