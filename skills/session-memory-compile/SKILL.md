---
type: skill
name: session-memory-compile
argument-hint: "[--all | --file path/to/daily.md | --dry-run]"
description: Wrapper around scripts/compile_memory.py — distill knowledge/daily logs into durable knowledge/notes pages and refresh index + log.
disable-model-invocation: true
allowed-tools: Read Glob Grep LS Bash(uv run python scripts/compile_memory.py *) Bash(python scripts/compile_memory.py *) Bash(uv run python scripts/rebuild_memory_index.py)
title: "SKILL"
timestamp: 2026-07-03T05:41:37
---
Run the scripted compile pass. The script uses the unified llm_client (`scripts/llm_client.py`) under the hood and follows `docs/AGENTS.md`.

Procedure:
1. Run `uv run python scripts/compile_memory.py $ARGUMENTS`.
   - no args = compile only daily logs whose hash changed since last compile
   - `--all` = compile every daily log
   - `--file knowledge/daily/YYYY-MM-DD.md` = compile one specific daily log
   - `--dry-run` = plan only, no writes, no state or log updates
2. The script already:
   - reads `docs/AGENTS.md`, `knowledge/index.md`, `knowledge/log.md`, existing knowledge pages
    - writes/updates pages under `knowledge/notes/`
    - runs `scripts/rebuild_memory_index.py`
    - appends a dated entry to `knowledge/log.md`
    - records compiled hashes in `$LLM_WIKI_STATE_ROOT/run/state.json` (gitignored, inside the vault)
3. Read the script's `COMPILE_DONE:` line to see which pages were touched.
4. If the result is unsatisfying (e.g. a daily log held material the script did not lift), make targeted Edit/Write changes manually, then re-run `uv run python scripts/rebuild_memory_index.py` and append a corrective entry to `knowledge/log.md`.

Return:
- which daily logs were processed
- which pages were created or updated
- any unresolved questions or ambiguous decisions
