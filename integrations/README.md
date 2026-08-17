# IDE Agent Integration

MCP is the common read/action interface for every compatible agent. Native
hooks, plugins, wrappers, and rules remain thin lifecycle or guidance adapters;
they do not implement a second memory API.

## Cursor

Configure the local MCP server for reads and actions, install Cursor locally, then rerun
the native LLM Wiki installer. It structurally owns only the exact LLM-Wiki handlers in
`~/.cursor/hooks.json`; unrelated events, handlers, and keys are preserved. The hooks
normalize `sessionStart`, `beforeSubmitPrompt`, significant `postToolUse`, `preCompact`,
`stop`, and `sessionEnd` through `scripts/integration_adapter.py`.

`integrations/cursor/rules/llm-wiki.mdc` remains optional agent guidance. It is not the
capture transport. Cursor cloud agents do not load user-level hooks, so automatic
Cursor capture is local only.

## Antigravity

Configure the local MCP server for reads and actions, install Antigravity locally, then
rerun the native LLM Wiki installer. It structurally owns only the top-level `llm-wiki`
fragment in `~/.gemini/config/hooks.json`; unrelated configuration is preserved.
`PreInvocation`, significant `PostToolUse`, and `Stop` events pass through the same
canonical integration adapter and occurrence-receipt path.

`integrations/antigravity/AGENTS.md` remains optional agent guidance. It does not append
daily Markdown directly and is not the capture transport.

Both managed configurations use bounded verified sibling preimages. Malformed JSON,
ownership conflicts, and drift block mutation. Doctor inspects structural ownership
without repairing user configuration.

## What works differently from CLI agents

IDE agents (Cursor, Antigravity; VS Code Copilot — planned, not yet implemented) work differently from CLI agents (OpenCode, Codex, Claude Code):

| Feature | CLI agents (OpenCode/Codex/Claude) | IDE agents (Cursor/Antigravity) |
|---|---|---|
| **Reads/actions** | 12 task-shaped MCP tools | The same 12 task-shaped MCP tools |
| **Auto-capture** | Thin hooks/plugins forward lifecycle events | Official local user hooks forward supported lifecycle events |
| **Session classification** | FLUSH MAJOR/MINOR/OK at idle | Supported stop/session-end events use the shared classification path |
| **Nightly compile** | Native scheduler (Task Scheduler, LaunchAgent, or user systemd); cron is explicit fallback | Same — vault is shared |
| **Context injection** | SessionStart hook injects bounded context | Supported local session-start hooks use the same bounded context builder |
| **LLM backend** | `llm_client.py` handles memory compilation | The shared vault uses the same memory backend; the IDE model remains host-managed |

**Key insight**: the vault is **shared infrastructure**. All agents write to the same `knowledge/daily/` and read from the same `knowledge/notes/`. A decision recorded by Cursor is visible to OpenCode in its next session.

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
