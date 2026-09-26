# The installers register Claude MCP alike

Date: 2026-09-26. Audit 2026-09-26, finding C-13 (the installers disagree on the
Claude MCP entry).

## What was wrong

`install.sh` and `install.ps1` read the `llm-wiki` entry of `~/.claude.json` the
same way (`missing`, `absent`, `current`, `elsewhere`, `unreadable`) but acted
and reported differently:

- For an existing file without the entry, `install.sh` asks Claude Code's own CLI
  to add it (`claude mcp add --scope user …`) and only falls back to printing the
  command; `install.ps1` only printed a JSON fragment to merge by hand.
- `install.ps1` reported "Claude Code: active automatic" for every state except
  `elsewhere` — including `absent` and `unreadable`, where no MCP server was
  registered. `install.sh` reports "active automatic" only for `current`.

## Decision

One contract, as install.sh already had it:

- `Get-ClaudeStatusLine` maps states exactly as `claude_status_line` does:
  `current` → active automatic, `elsewhere` → points at another vault, anything
  else → "hooks active; MCP server not registered".
- `Register-ClaudeMcp` runs `claude mcp add --scope user llm-wiki -- uv run …`
  when the CLI exists; on success the state is `current`, otherwise the same
  `claude mcp add` command is printed. The file is never rewritten by the
  installer while it exists (it is Claude Code's live state file).
- A file the installer creates is `current`.

Guards: every "Claude Code: …" status line in `install.sh` must exist in
`install.ps1`; both must carry the same `claude mcp add` command; and, where
PowerShell is present (CI runners; locally verified with portable PowerShell
7.6.6 on 2026-09-26), each state must print the same line in both installers.
Windows PowerShell 5.1 itself was not run: no 5.1 exists on Linux, and the CI
matrix runs `pwsh`; the functions changed use no 7-only syntax.

## Source

Claude Code documentation, MCP, https://code.claude.com/docs/en/mcp, fetched
2026-09-26: "User-scoped servers are stored in `~/.claude.json` and provide
cross-project accessibility, making them available across all projects on your
machine while remaining private to your user account." Example:
`claude mcp add --transport http hubspot --scope user https://mcp.hubspot.com/anthropic`.

## Files

- `install.ps1`
- `tests/test_the_installers_register_claude_mcp_alike.py`
