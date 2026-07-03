---
type: skill
name: knowledge-qa-file-back
argument-hint: "[optional slug hint]"
description: Promote a just-answered question into a durable Q&A page — either in `wiki/qa/` (external-domain questions about material in `raw/`) or `memory/knowledge/qa/` (internal "how we work here" questions).
disable-model-invocation: true
allowed-tools: Read Glob Grep LS Edit Write Bash(python scripts/rebuild_memory_index.py)
title: "SKILL"
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
Apply this two-question checklist:

1. **Would the answer be useful to someone who had never seen this repo's session history?**
   - *Yes* → it's external-domain knowledge. Candidate for `wiki/qa/`.
   - *No* → it's project lore. Candidate for `memory/knowledge/qa/`.

2. **Does the answer cite `raw/` material, public sources, or named authors?**
   - *Yes* → strengthens the `wiki/qa/` case.
   - *No, only internal files / conventions / hooks / scripts* → strengthens the `memory/knowledge/qa/` case.

If a question straddles both, default to `memory/knowledge/qa/` and link out to the relevant wiki concept(s) — it is easier to promote later via `bridge-promote-insight` than to demote.

## Procedure

1. **Pick the target tree** using the checklist above.

2. **Pick a slug** — kebab-case, phrased as the question's key phrase (not the answer).
   - Good: `inbox-vs-raw-after-compile`, `when-to-rebuild-qmd-index`
   - Bad: `question-about-inbox`, `answer-1`

3. **Write the page** at either:
   - `wiki/qa/<slug>.md` — follow wiki conventions (frontmatter with `type: qa`, `One-sentence summary:`, `Source:` citing `raw/` or prior wiki pages).
   - `memory/knowledge/qa/<slug>.md` — follow `memory/AGENTS.md` conventions (H1 title is the question verbatim, `One-sentence summary:`, `## Question`, `## Answer`, `## Evidence` pointing to the daily log + timestamp, `## Related`).

4. **Register and log**:
   - If target is `wiki/qa/`: ensure a `### Q&A` section exists under `## Main areas` in `wiki/index.md` (create it if missing), add the new page there, then append a dated entry to `wiki/log.md`.
   - If target is `memory/knowledge/qa/`: run `python scripts/rebuild_memory_index.py` (the index is auto-generated), then append a dated entry to `memory/log.md`.

5. **Cross-link**:
   - Add a wikilink to the new Q&A page from the most closely related concept/workflow page on the same side.
   - If the Q&A sits in `memory/` but references a wiki concept, link to the wiki concept in `## Related`.

## Anti-patterns
- Don't invent a Q the user did not actually ask. File-back records real Q&A, not imagined FAQ material.
- Don't duplicate the answer across both trees. Pick one home; cross-link from the other.
- Don't skip step 4 — an unregistered Q&A page will be flagged as `orphan_pages` by `lint_memory.py`.

## Return
- The path of the created page.
- The routing decision and why (one line).
- Any follow-ups (e.g. "consider promoting via bridge-promote-insight after N more related questions").
