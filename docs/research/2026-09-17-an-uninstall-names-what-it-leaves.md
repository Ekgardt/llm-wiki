# An uninstall names what it leaves

Dated 2026-09-17. Finding I-A7 of the third audit (medium-low, confirmed by reading). The
research before the fix.

## What was found

- The installers register the MCP server with each agent outside the ownership transaction:
  `~/.claude.json` (through `claude mcp add`, or a new file), `~/.codex/config.toml` (an
  appended block, with `config.toml.bak`), and OpenCode's user config (`mcp.llm-wiki`, with
  a `*.llm-wiki.<sha>.bak`). An uninstall takes none of them back and says nothing: after
  the vault is deleted each agent starts a server in a directory that is gone.
- `install_control.unowned_install_paths` already exists for this kind of thing — "reported
  instead of owned: after an uninstall the operator is told exactly what is still there" —
  but it names only the checkout and `.venv`, and only `status` prints it.
- `install.ps1` never passes `--opencode-plugin`, so on Windows the OpenCode plugin is
  written by `installer_config.configure_opencode` alone and no uninstall removes it. The
  POSIX installer was fixed for exactly this on an earlier date.

## Practice on this date

- These three files belong to the agents. `~/.claude.json` is Claude Code's live state and
  is rewritten while it runs; the installer already refuses to read-modify-write it and
  uses the agent's own CLI. Owning such a file in a transaction with preimages would
  restore a stale copy over the agent's newer state. Debian Policy 6.2 says an idempotent script
  "just ensures that everything is the way it ought to be" and does no harm
  (<https://www.debian.org/doc/debian-policy/ch-maintainerscripts.html>, fetched today);
  for a file somebody else keeps writing, the harmless form is to name the entry and the
  command that removes it.

## The decision

- `unowned_agent_registrations(home)` lists the agent files that still carry an `llm-wiki`
  entry, each with the way to remove it. `status` and the result of `uninstall` both carry
  it next to the unowned paths. Nothing is deleted.
- `install.ps1` detects OpenCode the way `install.sh` does and passes `--opencode-plugin`,
  so the transaction owns the plugin on Windows too.

Files: `scripts/install_control.py`, `install.ps1`,
`tests/test_an_uninstall_names_what_it_leaves.py`,
`docs/research/2026-09-17-an-uninstall-names-what-it-leaves.md`.
