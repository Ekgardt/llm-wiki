# Every memory tool is visible from the first turn

Date: 2026-11-17. Approved by the owner as item 4 of the retrieval list
("all tools visible").

## What was wrong

Claude Code and Codex both hide MCP tools behind a tool search by default. An
agent saw only a tool-search entry until it searched for the memory, so a
session that never thought of searching never saw `recall`. One extra model
round trip was spent on every first use.

## What changed

- `scripts/mcp_server.py` lists every tool with
  `_meta: {"anthropic/alwaysLoad": true}`. Claude Code loads such a tool with
  the session instead of deferring it.
- `install.sh` and `install.ps1` write `omit_tools_from = ["deferred"]` into
  the Codex entry they add. Codex then puts the tools in the initial list.
- `config.toml` is never rewritten in place. `codex_memory.py config-state`
  answers `deferred` for an entry that is ours but does not list `deferred` in
  `omit_tools_from`. Both installers then say to add `"deferred"` to the entry's
  `omit_tools_from` list, or the whole line when the entry has none: the entry may
  already list other surfaces, and TOML refuses a second key of the same name.
- `tests/test_every_memory_tool_is_visible.py` pins all of this and a size
  budget. The model-facing part of the 12 tools (name, description, input
  schema) measured 10 755 bytes, about 2 700 tokens; the budget is 12 KiB.
  Loading tools up front costs that every turn, which is why Claude Code's
  documentation advises it only for tools the agent needs on every turn.

## Alternatives considered

1. **Leave the tools deferred.** Rejected: the owner asked for every tool to be
   visible, and a memory the agent does not see is not consulted.
2. **Mark only `recall` always-loaded.** Fewer tokens per turn, but the other
   eleven stay hidden, which the request rules out.
3. **Per-server `alwaysLoad` in Claude Code's configuration.** It also works,
   but the installer writes `~/.claude.json` only when it is missing, and
   `claude mcp add` sets no such field. The per-tool key travels with the
   server and needs no change to the user's file.
4. **`ENABLE_TOOL_SEARCH=false`.** Turns deferral off for every server the user
   has, not only this one; not ours to decide.

## Limits

- Claude Code issue #82900 reports the per-tool key had no effect for HTTP
  servers, and #56551 that plugin servers ignore it. This server is installed as
  a stdio server, which neither report covers. If an installed Claude Code still
  defers the tools, per-server `alwaysLoad` (alternative 3) is the next step.
- Codex accepts `omit_tools_from` from 0.150 on (stated by the awslabs notes;
  the official reference documents the key but gives no version).
- A user whose Claude Code entry sets `"alwaysLoad": false` keeps the tools
  deferred; the user's configuration outranks the server's request.
- An existing Codex entry is reported, not changed: the user adds the line, or
  reruns the installer to see it again. Doctor does not check it yet.

## Sources

- Claude Code documentation, "Connect Claude Code to tools via MCP", section
  "Mark a tool to load upfront or stay deferred" (a tool whose `_meta` sets
  `"anthropic/alwaysLoad": true` loads upfront; a server entry with
  `"alwaysLoad": false` still defers it) and "Exempt a server from deferral"
  (the every-turn cost): https://code.claude.com/docs/en/mcp
- Claude Code issues #82900 and #56551 (limits of the per-tool key):
  https://github.com/anthropics/claude-code/issues/82900,
  https://github.com/anthropics/claude-code/issues/56551
- OpenAI Codex configuration reference, `mcp_servers.<id>.omit_tools_from`
  ("Choose `direct` (initial tools), `deferred` (tool search), or `code_mode`"):
  https://learn.chatgpt.com/docs/config-file/config-reference (the former
  https://developers.openai.com/codex/config-reference redirects there)
- awslabs cli-agent-orchestrator, Codex notes (`omit_tools_from=["deferred"]`
  from Codex 0.150): https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/codex-cli.md
- manaflow-ai/cmux pull request #39119 (the per-tool key in use):
  https://github.com/manaflow-ai/cmux/pull/39119
