---
type: debugging
title: "Hook Errors Silent Without LLM_WIKI_STATE_ROOT"
description: "When `LLM_WIKI_STATE_ROOT` is absent from `~/.claude/settings.json::env`, hook scripts cannot locate the error log and silently swallow all failures — 'no errors in hook-errors.log' does not mean the "
timestamp: 2026-07-03T05:41:37
---
# Hook Errors Silent Without LLM_WIKI_STATE_ROOT

One-sentence summary: When `LLM_WIKI_STATE_ROOT` is absent from `~/.claude/settings.json::env`, hook scripts cannot locate the error log and silently swallow all failures — "no errors in hook-errors.log" does not mean the hooks ran cleanly.

## Symptom / Cause / Resolution

**Symptom:** Hooks appear to succeed (exit 0, no log entries), but expected side-effects (state.md creation, daily-log append) are missing or wrong.

**Cause:** Both `session_start_project_state.py` and `session_end_project_tag.py` call `_safe_write_error()` to record failures. That function constructs the path to `hook-errors.log` using `LLM_WIKI_STATE_ROOT`. If the env var is absent from the user-level `settings.json::env` block (easy to forget because it is separate from the PATH), `_safe_write_error` cannot resolve the log path and discards the error silently. The hook exits 0 regardless (all hooks are fail-safe), so Claude Code sees no sign of trouble.

**Resolution:** Both scripts now carry `_resolve_state_root()` — a private fallback that derives the state root from `$LLM_WIKI_ROOT/../LLM-wiki-state` when `LLM_WIKI_STATE_ROOT` is unset. This covers the common case where only `LLM_WIKI_ROOT` is configured. However, the correct fix is to add both env vars explicitly to `~/.claude/settings.json::env`:

```json
"env": {
  "LLM_WIKI_ROOT": "$LLM_WIKI_ROOT",
  "LLM_WIKI_STATE_ROOT": "$LLM_WIKI_ROOT-state"
}
```

**Verification:** After editing `settings.json`, confirm `hook-errors.log` is writable by running a hook manually:

```bash
CLAUDE_PROJECT_DIR=<your-project> python scripts/session_start_project_state.py
```

## Evidence
- `knowledge/daily/2026-04-19.md` [17:09:56] — Phase 4 soak test surfaced the silent-error bug
- `knowledge/daily/2026-04-19.md` [17:24:33] — fix (`_resolve_state_root`) confirmed and committed

## Related
- [[knowledge/notes/hook-scripts-defense-in-depth]] — broader defense-in-depth decision covering this fix + slug guard
- [[knowledge/notes/b-sim-hook-testing]] — technique for exercising hook scripts manually
