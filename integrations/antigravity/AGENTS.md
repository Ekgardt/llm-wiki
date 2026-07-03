# LLM-Wiki Memory Integration

You have access to a persistent memory vault that stores decisions, lessons, and patterns from all coding sessions. This memory persists across Cursor, OpenCode, Codex, Claude Code, and Antigravity sessions.

## Setup

Set the vault root (add to your shell profile):
```bash
export LLM_WIKI_ROOT="/path/to/LLM-wiki"
```

## Commands

### Recall past knowledge
```bash
uv run python "$LLM_WIKI_ROOT/scripts/search_memory.py" "auth decision" --limit 5
uv run python "$LLM_WIKI_ROOT/scripts/search_memory.py" "database performance" --semantic --limit 5
```

### Read project state
```bash
cat "$LLM_WIKI_ROOT/wiki/projects/<project-slug>/state.md"
```

### Record a decision
```bash
uv run python "$LLM_WIKI_ROOT/scripts/daily_log_append.py" << 'EOF'
{"slug":"<project-slug>","sessionId":"antigravity","block":"## [TIMESTAMP] antigravity-session | decision\n- Decision: <what>\n- Reason: <why>"}
EOF
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
3. **Never edit wiki/ or memory/knowledge/ files directly** — use scripts
4. **Read guard rails at start** — prevents repeating past mistakes
