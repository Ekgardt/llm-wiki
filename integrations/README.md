# IDE Agent Integration

MCP is the common read/action interface for every compatible agent. Native
hooks, plugins, wrappers, and rules remain thin lifecycle or guidance adapters;
they do not implement a second memory API.

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
| **Reads/actions** | 12 task-shaped MCP tools | The same 12 task-shaped MCP tools |
| **Auto-capture** | Thin hooks/plugins forward lifecycle events | Depends on host lifecycle support |
| **Session classification** | FLUSH MAJOR/MINOR/OK at idle | Manual: agent records when told |
| **Nightly compile** | Scheduler (Task Scheduler on Windows, cron on Unix) runs automatically | Same — vault is shared |
| **Context injection** | SessionStart hook injects 2KB | Rules file tells agent to read files |
| **LLM backend** | llm_client.py (5 backends) | IDE's own LLM (Cursor Pro, Gemini) |

**Key insight**: the vault is **shared infrastructure**. All agents write to the same `knowledge/daily/` and read from the same `knowledge/notes/`. A decision recorded by Cursor is visible to OpenCode in its next session.

## MCP Server

`scripts/mcp_server.py` exposes 12 task-shaped tools over local stdio. The
installer baseline includes the MCP package. For manual dependency selection from
source, run `uv sync --locked --extra mcp-server`, then configure the
agent to run `uv run python scripts/mcp_server.py` from the vault root.

The tools include vault search, context, decisions, maintenance, conservative
code analysis, and `doctor`. Every tool returns the same versioned response
envelope; health and context are also MCP resources. Automatic SessionStart
health output appears only for degraded/error checks.

## Obsidian

Obsidian is an optional Markdown viewer. Open the vault directly if desired.
There is no bundled ingestion wiring, required UI, or canonical frontend.
