---
type: skill
name: session-memory-review
argument-hint: "[optional area]"
description: Review the health of session memory — first runs scripts/lint_memory.py --structural-only, then LLM-reviews the structural findings for staleness, duplication, and missing durable decisions.
disable-model-invocation: true
context: fork
allowed-tools: Read Glob Grep LS Bash(uv run python scripts/lint_memory.py *) Bash(python scripts/lint_memory.py *)
title: "SKILL"
timestamp: 2026-07-03T05:41:37
---
Review `$ARGUMENTS` if provided, otherwise review the whole knowledge subsystem (`knowledge/daily/`, `knowledge/notes/`).

Procedure:
1. **Structural pass (scripted):**
   Run `uv run python scripts/lint_memory.py --structural-only`.
   The script writes a report to `$LLM_WIKI_STATE_ROOT/logs/lint-YYYY-MM-DD.md` covering:
   - broken wikilinks across `knowledge/notes/`
   - orphan knowledge pages (not referenced from the relevant `index.md`)
   - orphan daily logs (not yet compiled)
   - stale compiled pages (daily hash drifted after last compile)
   - missing backlinks between related pages
   - sparse pages (under 200 words by default; configurable via `--sparse-words`)
   - opt-in LLM-judged contradictions via `--contradictions`

2. **LLM review pass (over the scripted findings):**
   Read the lint report, then answer:
   - Are there duplicate or near-duplicate decision/pattern pages that should be merged?
   - Are debugging pages turning into stale incident notes that should be archived?
   - Are there recurring themes across multiple daily logs that have not yet been lifted into knowledge?
   - Are there weak links between `knowledge/notes/` and `knowledge/notes/` that would benefit from `bridge-promote-insight`?

Return:
- concise health report summarizing the structural findings
- top 5 cleanup or promotion actions (ordered by impact)
- exact files involved
