# IDE Agent Integration

Cursor and Antigravity (and other MCP-compatible IDEs) can access the LLM-Wiki memory vault through rules files and CLI tools.

## Cursor

1. Copy the rules file to your project:
```bash
cp integrations/cursor/rules/llm-wiki.mdc /path/to/your/project/.cursor/rules/
```

2. Set the vault root:
```bash
export LLM_WIKI_ROOT="/path/to/LLM-wiki"
```

Cursor will now:
- Read project state at session start
- Search the vault when you ask about past decisions
- Record new decisions to the daily log
- Show guard rails (learned rules from corrections)

## Antigravity

1. Copy the AGENTS.md to your project:
```bash
cp integrations/antigravity/AGENTS.md /path/to/your/project/
```

2. Or append to an existing AGENTS.md.

3. Set the vault root (same as Cursor).

## What works differently from CLI agents

IDE agents (Cursor, Antigravity; VS Code Copilot — planned, not yet implemented) work differently from CLI agents (OpenCode, Codex, Claude Code):

| Feature | CLI agents (OpenCode/Codex/Claude) | IDE agents (Cursor/Antigravity) |
|---|---|---|
| **Auto-capture** | Hooks/plugins fire automatically | Agent must call scripts via Bash |
| **Session classification** | FLUSH MAJOR/MINOR/OK at idle | Manual: agent records when told |
| **Nightly compile** | Scheduler (Task Scheduler on Windows, cron on Unix) runs automatically | Same — vault is shared |
| **Context injection** | SessionStart hook injects 2KB | Rules file tells agent to read files |
| **LLM backend** | llm_client.py (5 backends) | IDE's own LLM (Cursor Pro, Gemini) |

**Key insight**: the vault is **shared infrastructure**. All agents write to the same `knowledge/daily/` and read from the same `knowledge/notes/`. A decision recorded by Cursor is visible to OpenCode in its next session.

## MCP Server (planned — not yet implemented)

An MCP server surface for `search_memory.py` / `query_memory.py` is on the
roadmap. Today, agents integrate via the hooks / plugin / rules mechanisms
described above. When the MCP server lands, this section will show a
canonical `mcpServers` config block.

(MCP server mode is planned — currently use the Bash commands above.)
