# LLM-Wiki Memory Integration

You have access to a persistent memory vault that stores decisions, lessons, and patterns from all coding sessions. This memory persists across Cursor, OpenCode, Codex, Claude Code, and Antigravity sessions.

> **Platform note:** the shell commands below are POSIX (bash/zsh). On Windows, use PowerShell equivalents (e.g. `type` instead of `cat`, `$env:LLM_WIKI_ROOT` instead of `$LLM_WIKI_ROOT`).

## Setup

Set the vault root (add to your shell profile):
```bash
export LLM_WIKI_ROOT="/path/to/LLM-wiki"
```

## At session start (MANDATORY — do this first)

Read the session context file for your current knowledge state:
```bash
cat "$LLM_WIKI_ROOT/cache/session-context.md" 2>/dev/null
```
If it doesn't exist, generate it:
```bash
mkdir -p "$LLM_WIKI_ROOT/cache" && uv run python "$LLM_WIKI_ROOT/scripts/session_start_context.py" --output-file "$LLM_WIKI_ROOT/cache/session-context.md" 2>/dev/null && cat "$LLM_WIKI_ROOT/cache/session-context.md"
```

## Commands

### Recall past knowledge
```bash
uv run python "$LLM_WIKI_ROOT/scripts/search_memory.py" "auth decision" --limit 5
uv run python "$LLM_WIKI_ROOT/scripts/search_memory.py" "database performance" --semantic --limit 5
```

### Read project state
```bash
cat "$LLM_WIKI_ROOT/knowledge/projects/<project-slug>/state.md"
```

### Record a decision
```bash
TS=$(date +%H:%M:%S)
echo '{"slug":"<project-slug>","sessionId":"antigravity","block":"## ['"$TS"'] antigravity-session | decision\n- Decision: <what>\n- Reason: <why>"}' | uv run python "$LLM_WIKI_ROOT/scripts/daily_log_append.py"
```

### Get guard rails (learned rules)
```bash
uv run python "$LLM_WIKI_ROOT/scripts/build_guardrails.py"
```

### Get advisory (open threads, last decision)
```bash
uv run python "$LLM_WIKI_ROOT/scripts/build_advisory.py" "<project-slug>"
```

## Rules

1. **Search vault before architecture decisions** — past sessions may have solved this
2. **Record non-trivial decisions** — use daily_log_append.py
3. **Never edit knowledge/notes/ or knowledge/daily/ files directly** — use scripts
4. **Read guard rails at start** — prevents repeating past mistakes
