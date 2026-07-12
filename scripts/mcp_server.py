"""LLM-Wiki MCP Server — 9 task-shaped tools, stdio transport, 100% local.

Gives AI agents (Claude Code, OpenCode, Codex, Cursor, Antigravity) structured
access to the knowledge vault via Model Context Protocol. No server, no cloud,
no network — stdio subprocess on the same machine.

Install: uv sync --extra mcp-server
Run:    uv run python scripts/mcp_server.py

Agent config (e.g. for Claude Code ~/.claude/.mcp.json):
{
  "mcpServers": {
    "llm-wiki": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/llm-wiki", "python", "scripts/mcp_server.py"]
    }
  }
}

Tools (task-shaped, not entity-shaped — see repowise design):
  recall(query, limit)       — hybrid search (BM25 + vector + graph + reranker)
  read_page(slug)            — full page content (the only raw-bytes tool)
  wiki_overview()            — vault stats, page count, retrieval tier
  get_context(slugs, include) — batch: neighbors, backlinks, provenance
  get_decisions(query)       — active architectural decisions
  vault_status()             — metacognitive block (gaps, backlog, stale)
  log_decision(summary)      — append a decision to daily log
  check_contradiction(claim) — find conflicting pages
  compile(scope)             — trigger compile (non-blocking)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MCP_AVAILABLE = False
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
    MCP_AVAILABLE = True
except ImportError:
    pass


def _search_vault(query: str, limit: int = 8) -> list[dict]:
    """Run hybrid search on the vault."""
    from search_memory import search
    return search(query, limit=limit)


def _read_page(slug: str) -> dict:
    """Read a full page by slug."""
    from memory_state import ROOT
    page_path = ROOT / "knowledge" / "notes" / f"{slug}.md"
    if not page_path.exists():
        return {"error": f"Page not found: {slug}"}
    content = page_path.read_text(encoding="utf-8")
    return {"slug": slug, "path": str(page_path.relative_to(ROOT)), "content": content}


def _wiki_overview() -> dict:
    """Get vault statistics and retrieval tier recommendation."""
    from lookup_mode import count_wiki_pages, tier_for
    count = count_wiki_pages()
    tier = tier_for(count)
    return {
        "page_count": count,
        "retrieval_tier": tier,
        "vault_root": str(Path(__file__).resolve().parent.parent),
    }


def _vault_status() -> dict:
    """Metacognitive block: gaps, compile backlog, stale pages."""
    from memory_state import load_state
    state = load_state()
    return {
        "last_compile": state.get("last_compile_at", "never"),
        "last_compile_status": state.get("last_compile_status", "unknown"),
        "compile_backlog": len(state.get("compiled_daily_hashes", {})),
    }


def _get_decisions(query: str | None = None, limit: int = 10) -> list[dict]:
    """Get active decisions from the vault."""
    from search_memory import search
    results = search(query or "decision", limit=limit)
    # Filter to decision-type pages
    return [r for r in results if r.get("type") == "decision" or "decision" in r.get("path", "").lower()]


def _get_context(slugs: list[str], include: list[str] | None = None) -> dict:
    """Batch context for multiple pages."""
    include = include or []
    result = {}
    for slug in slugs:
        page = _read_page(slug)
        if "error" not in page:
            entry = {"slug": slug, "title": slug}
            if "content" in page and "frontmatter" in (include or []):
                entry["content_preview"] = page["content"][:500]
            result[slug] = entry
    return result


def _check_contradiction(claim: str) -> list[dict]:
    """Find pages that might contradict a claim."""
    from search_memory import search
    return search(claim, limit=5)


def _log_decision(summary: str, rationale: str = "") -> dict:
    """Append a decision to the daily log."""
    from datetime import datetime

    from daily_log_append import append_daily
    from memory_state import ROOT

    slug = "manual-decision"
    now = datetime.now()
    block = f"\n## [{now.strftime('%H:%M:%S')}] manual decision\n"
    block += f"Trigger: manual\nslug: {slug}\nroot: {ROOT}\n\n"
    block += f"Decision: {summary}\n"
    if rationale:
        block += f"Rationale: {rationale}\n"

    try:
        path = append_daily(now.strftime("%Y-%m-%d"), slug, block)
        return {"status": "logged", "path": str(path)}
    except Exception as e:
        return {"error": str(e)}


def _trigger_compile() -> dict:
    """Trigger a compile (non-blocking)."""
    from maybe_compile import spawn_compile_if_idle
    spawned, reason = spawn_compile_if_idle()
    return {"spawned": spawned, "reason": reason}


def _build_tool_definitions() -> list:
    """Build MCP tool definitions."""
    if not MCP_AVAILABLE:
        return []

    return [
        Tool(
            name="recall",
            description="Search the knowledge vault. Returns ranked results with titles, summaries, and paths. Use this to find relevant knowledge pages.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "default": 8, "description": "Max results (1-20)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="read_page",
            description="Read a full knowledge page by its slug (filename without .md). Use this when you need the complete content of a specific page.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Page slug (e.g. 'auth-decision')"},
                },
                "required": ["slug"],
            },
        ),
        Tool(
            name="wiki_overview",
            description="Get vault statistics: page count, retrieval tier recommendation, vault root path. Call this first in any session.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="vault_status",
            description="Get metacognitive status: last compile, compile backlog, stale pages. Use to check if maintenance is needed.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_decisions",
            description="Find active architectural decisions in the vault. Optionally filter by query.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Optional filter query"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_context",
            description="Get context for multiple pages at once. Batch operation for efficiency.",
            inputSchema={
                "type": "object",
                "properties": {
                    "slugs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of page slugs",
                    },
                    "include": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra fields: 'frontmatter', 'neighbors'",
                    },
                },
                "required": ["slugs"],
            },
        ),
        Tool(
            name="check_contradiction",
            description="Check if a claim contradicts existing knowledge. Returns potentially conflicting pages.",
            inputSchema={
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "description": "The claim to check"},
                },
                "required": ["claim"],
            },
        ),
        Tool(
            name="log_decision",
            description="Log a new decision to the daily log. Triggers compile on next session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Decision summary"},
                    "rationale": {"type": "string", "description": "Why this decision was made"},
                },
                "required": ["summary"],
            },
        ),
        Tool(
            name="compile",
            description="Trigger a background compile of daily logs into knowledge pages. Non-blocking.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


async def _handle_tool_call(name: str, arguments: dict) -> str:
    """Handle a tool call and return text result."""
    import json

    try:
        if name == "recall":
            results = _search_vault(
                arguments.get("query", ""),
                limit=arguments.get("limit", 8),
            )
            return json.dumps(results, indent=2, ensure_ascii=False)

        elif name == "read_page":
            result = _read_page(arguments.get("slug", ""))
            return json.dumps(result, indent=2, ensure_ascii=False)

        elif name == "wiki_overview":
            result = _wiki_overview()
            return json.dumps(result, indent=2, ensure_ascii=False)

        elif name == "vault_status":
            result = _vault_status()
            return json.dumps(result, indent=2, ensure_ascii=False)

        elif name == "get_decisions":
            results = _get_decisions(
                arguments.get("query"),
                limit=arguments.get("limit", 10),
            )
            return json.dumps(results, indent=2, ensure_ascii=False)

        elif name == "get_context":
            result = _get_context(
                arguments.get("slugs", []),
                arguments.get("include"),
            )
            return json.dumps(result, indent=2, ensure_ascii=False)

        elif name == "check_contradiction":
            results = _check_contradiction(arguments.get("claim", ""))
            return json.dumps(results, indent=2, ensure_ascii=False)

        elif name == "log_decision":
            result = _log_decision(
                arguments.get("summary", ""),
                arguments.get("rationale", ""),
            )
            return json.dumps(result, indent=2, ensure_ascii=False)

        elif name == "compile":
            result = _trigger_compile()
            return json.dumps(result, indent=2, ensure_ascii=False)

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


def run_server() -> int:
    """Start the MCP server (stdio transport). Returns exit code."""
    if not MCP_AVAILABLE:
        print(
            "MCP package not installed. Run: uv sync --extra mcp-server",
            file=sys.stderr,
        )
        return 1

    import asyncio

    server = Server("llm-wiki")
    tools = _build_tool_definitions()

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        result = await _handle_tool_call(name, arguments)
        return [TextContent(type="text", text=result)]

    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(main())
    return 0


if __name__ == "__main__":
    raise SystemExit(run_server())
