---
type: skill
name: knowledge-compile
argument-hint: "[path or topic]"
description: Compile inbox or raw material into durable wiki pages, update the wiki index, and append a log entry.
disable-model-invocation: true
allowed-tools: Read Glob Grep LS Edit Write Bash(uv run python scripts/search_memory.py *) Bash(uv run python scripts/rebuild_memory_index.py *)
title: "SKILL"
timestamp: 2026-07-03T05:41:37
---
Compile `$ARGUMENTS` into the wiki.

Procedure:
1. Identify the relevant source file(s) in `knowledge/inbox/` and/or `knowledge/raw/`.
2. Inspect `knowledge/index.md` and any likely existing pages first.
3. Decide whether to:
   - create new pages under `knowledge/notes/` (flat slug)
   - or update existing pages.
4. For every durable page you create/update:
   - add a one-sentence summary near the top
   - add concise synthesis, not raw dumping
   - include a `Source:` section with file paths
   - add related wikilinks.
5. Update `knowledge/index.md` so the page is discoverable.
6. Append one concise line to `knowledge/log.md` with date + what changed.
7. After the wiki writes succeed, refresh the search index so search stays current:
   - run `uv run python scripts/search_memory.py --rebuild` (rebuilds FTS5 + vector cache)
   - if the command fails, report the stderr in the summary but do not roll back the wiki edits.
8. Return a short summary of:
   - source files used
   - pages created/updated
   - search index refresh result (one line: `search_memory --rebuild: ok` or the error)
   - unresolved questions or contradictions.
