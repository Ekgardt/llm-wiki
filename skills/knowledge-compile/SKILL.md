---
type: skill
name: knowledge-compile
argument-hint: "[path or topic]"
description: Compile inbox or raw material into durable wiki pages, update the wiki index, and append a log entry.
disable-model-invocation: true
allowed-tools: Read Glob Grep LS Edit Write Bash(qmd *) Bash(qmd update) Bash(qmd embed)
title: "SKILL"
timestamp: 2026-07-03T05:41:37
---
Compile `$ARGUMENTS` into the wiki.

Procedure:
1. Identify the relevant source file(s) in `knowledge/inbox/` and/or `knowledge/raw/`.
2. Inspect `knowledge/index.md` and any likely existing pages first.
3. Decide whether to:
   - create new pages under `knowledge/notes/` (flat slug or typed subdir)
   - or update existing pages.
4. For every durable page you create/update:
   - add a one-sentence summary near the top
   - add concise synthesis, not raw dumping
   - include a `Source:` section with file paths
   - add related wikilinks.
5. Update `knowledge/index.md` so the page is discoverable.
6. Append one concise line to `knowledge/log.md` with date + what changed.
7. After the wiki writes succeed, refresh the QMD index so search stays current:
   - run `qmd update` (re-scans collections, picks up new/changed files)
   - run `qmd embed` (regenerates vector embeddings for changed files)
   - if either command fails, report the stderr in the summary but do not roll back the wiki edits.
8. Return a short summary of:
   - source files used
   - pages created/updated
   - QMD refresh result (one line: `qmd update/embed: ok` or the error)
   - unresolved questions or contradictions.

Use `qmd search` or `qmd query` only when the wiki is already large enough that direct reading is inefficient.

Note: steps 7–8 replace the old manual PowerShell follow-up (`scripts/qmd.ps1 update` / `embed`). The `qmd` CLI is on PATH and callable directly from bash, so no PowerShell wrapper is needed.
