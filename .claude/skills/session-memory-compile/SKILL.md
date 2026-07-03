---
type: skill
name: session-memory-compile
argument-hint: "[--all | --file path/to/daily.md | --dry-run]"
description: Wrapper around scripts/compile_memory.py — distill memory/daily logs into durable memory/knowledge pages and refresh index + log.
disable-model-invocation: true
allowed-tools: Read Glob Grep LS Bash(uv run python scripts/compile_memory.py *) Bash(python scripts/compile_memory.py *) Bash(uv run python scripts/rebuild_memory_index.py)
title: "SKILL"
timestamp: 2026-07-03T05:41:37
---
Run the scripted compile pass. The script uses the Claude Agent SDK under the hood and follows `memory/AGENTS.md`.

Procedure:
1. Run `uv run python scripts/compile_memory.py $ARGUMENTS`.
   - no args = compile only daily logs whose hash changed since last compile
   - `--all` = compile every daily log
   - `--file memory/daily/YYYY-MM-DD.md` = compile one specific daily log
   - `--dry-run` = plan only, no writes, no state or log updates
2. The script already:
   - reads `memory/AGENTS.md`, `memory/index.md`, `memory/log.md`, existing knowledge pages
   - writes/updates pages under `memory/knowledge/{concepts,decisions,patterns,debugging,qa}/`
   - runs `scripts/rebuild_memory_index.py`
   - appends a dated entry to `memory/log.md`
   - records compiled hashes in `$LLM_WIKI_STATE_ROOT/memory-state/state.json` (default `$LLM_WIKI_STATE_ROOT/memory-state\state.json`, outside the vault)
3. Read the script's `COMPILE_DONE:` line to see which pages were touched.
4. If the result is unsatisfying (e.g. a daily log held material the script did not lift), make targeted Edit/Write changes manually, then re-run `uv run python scripts/rebuild_memory_index.py` and append a corrective entry to `memory/log.md`.

Return:
- which daily logs were processed
- which pages were created or updated
- any unresolved questions or ambiguous decisions
