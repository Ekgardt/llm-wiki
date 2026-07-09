---
type: skill
name: contradict-check
argument-hint: "[--scope memory|wiki|all]  (default: all)"
description: LLM-judged contradiction check across the vault. Run before committing changes that touch knowledge pages, or as a git pre-commit hook.
disable-model-invocation: true
allowed-tools: Read Glob Grep LS Bash(python scripts/lint_memory.py *) Bash(uv run python scripts/lint_memory.py *)
title: "Only run the expensive check if knowledge pages changed."
timestamp: 2026-07-03T05:41:37
---
Wraps `scripts/lint_memory.py --contradictions`, which asks the Claude Agent SDK to flag concrete logical contradictions between pages.

## When to use
- Before committing a batch of edits to `knowledge/notes/` or `knowledge/notes/concepts|syntheses|comparisons|connections|qa/`.
- After promoting a page via `bridge-promote-insight` (to check the promoted page hasn't drifted from its memory origin).
- Scheduled: part of a weekly review, alongside `knowledge-review` / `session-memory-review`.
- As a git pre-commit hook (see *Installing as a pre-commit hook* below).

## What it catches
Only **concrete** contradictions — two pages giving different answers to the same question; a rule stated one way here and the opposite way there; a dated decision on page A silently violated by page B. It does **not** flag stylistic drift, differing levels of detail, or mere scope overlap — those belong in `knowledge-review`.

## Procedure

1. Run: `python scripts/lint_memory.py --contradictions $ARGUMENTS`.
   - `--scope all` (default) — knowledge/notes.
   - `--scope memory` / `--scope wiki` — one tree only.
   - Note: this check costs API calls; the other 6 structural checks are free and always run.

2. Open the generated report at `$LLM_WIKI_STATE_ROOT/logs/lint-YYYY-MM-DD.md` (default `$LLM_WIKI_STATE_ROOT/logs/`) and read the `## Contradictions` section.

3. For each finding:
   - If real: pick one page as canonical, update the other to defer to it, and add a `knowledge/log.md` or `knowledge/log.md` entry noting the resolution.
   - If false positive: record in the relevant knowledge page's `Related` section that the apparent conflict is scope-difference, not contradiction — this teaches future lints (by making the distinction explicit in-page).

4. Re-run the structural lint (`python scripts/lint_memory.py`) to confirm no new breakage from your fix.

## Installing as a pre-commit hook
Create `.git/hooks/pre-commit` (or add to an existing one):

```bash
#!/usr/bin/env bash
# Only run the expensive check if knowledge pages changed.
if git diff --cached --name-only | grep -qE '^knowledge/notes/'; then
  python scripts/lint_memory.py --contradictions --scope all || exit 1
fi
```

Then `chmod +x .git/hooks/pre-commit`. The `grep -qE` gate keeps the hook cheap when the diff is code-only.

## Anti-patterns
- Don't run on every save — structural `lint_memory.py` is the cheap daily pass; contradictions is the deliberate one.
- Don't silence a finding by deleting one of the pages. Resolve via supersession (CLAUDE.md rule 7: track superseded claims, don't silently delete history).

## Return
- Count of contradictions found.
- Path of the lint report.
- One-line verdict: clean / resolved inline / needs human judgment.
