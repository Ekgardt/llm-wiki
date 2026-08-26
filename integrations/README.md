# Agent Integration

MCP is the common read/action interface for every compatible agent. Native
hooks, plugins, wrappers, and rules remain thin lifecycle or guidance adapters;
they do not implement a second memory API.

## Supported agents

Claude Code, OpenCode, and Codex CLI are the supported hosts. Each uses a thin
lifecycle adapter installed and owned by the install control plane, and each reads
and acts through the same MCP server.

Cursor and Antigravity were retired on 2026-08-26. The installer no longer detects
them, writes their hook configuration, or reports on them. `uninstall` and `rollback`
still take back a fragment written by an older install, so an existing user is not
left with a hook pointing at a vault nothing maintains. See
`knowledge/notes/retire-cursor-and-antigravity-decision.md`.

## What works differently from CLI agents

The vault is **shared infrastructure**. All agents write to the same
`knowledge/daily/` and read from the same `knowledge/notes/`. A decision recorded by
Claude Code is visible to OpenCode in its next session.

| Feature | Every supported agent (Claude Code / OpenCode / Codex) |
|---|---|
| **Reads/actions** | 12 task-shaped MCP tools |
| **Auto-capture** | Thin hooks/plugins forward lifecycle events |
| **Session classification** | FLUSH MAJOR/MINOR/OK at idle |
| **Nightly compile** | Native scheduler (Task Scheduler, LaunchAgent, or user systemd); cron is explicit fallback |
| **Context injection** | SessionStart hook injects bounded context |
| **LLM backend** | `llm_client.py` handles memory compilation |

Managed configurations use bounded verified sibling preimages. Malformed JSON,
ownership conflicts, and drift block mutation. Doctor inspects structural ownership
without repairing user configuration.

## MCP Server

`scripts/mcp_server.py` exposes 12 task-shaped tools over local stdio. The
installer baseline includes the MCP package; `mcp-server` is only a compatibility
alias. For manual dependency selection, a production install runs
`uv sync --locked --no-default-groups`.
Configure a POSIX-shell agent to run
`uv run --locked --no-sync --directory "$LLM_WIKI_ROOT" python scripts/mcp_server.py`.
For PowerShell use
`uv run --locked --no-sync --directory $env:LLM_WIKI_ROOT python scripts/mcp_server.py`.

The tools include vault search, context, decisions, maintenance, conservative
code analysis, and `doctor`. Every tool returns the same versioned response
envelope; health and context are also MCP resources. Automatic SessionStart
health output appears only for degraded/error checks.

Installed-vault Reliability V3 inspection is a separate read-only operator command:
`uv run --locked --no-sync python scripts/repair_installed_memory.py --check --json`.
Mutating adoption is not active until the compatible v3 queue writers and canonical
ownership protocol land; the current apply path fails closed and never rewrites agent
integration configuration.

## Obsidian

Obsidian is an optional Markdown viewer. Open the vault directly if desired.
There is no bundled ingestion wiring, required UI, or canonical frontend.
