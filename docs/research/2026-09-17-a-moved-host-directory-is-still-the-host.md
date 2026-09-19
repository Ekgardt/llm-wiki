# A moved host directory is still the host's

Dated 2026-09-17. Finding C-F6 of the third audit (medium for the users it touches, confirmed by
reading and grep). The research before the fix.

## What was found

- Capture reads a transcript only from directories it trusts. The list is written out three
  times — `integration_adapter._validated_capture_transcript_path`,
  `flush_memory._transcript_prefixes` and `backfill_sessions.DEFAULT_SOURCE_ROOTS` — and each
  copy says `~/.claude/projects` and `~/.codex/sessions`.
- Both hosts let the user move that directory, and nothing in `scripts/` reads either
  variable for this purpose. With a moved directory every `session_end` and `pre_compact`
  ends in `PermissionError: capture transcript path is not allowed`, and the backfill finds
  no sessions at all. The loss is at least written to the failure trail.

## Practice on this date

- Claude Code: "To keep the home-directory files somewhere else, set `CLAUDE_CONFIG_DIR`;
  Claude Code then stores your settings, session history, and plugins there instead"
  ([Claude Code settings](https://code.claude.com/docs/en/settings), fetched 2026-09-17).
- Codex: "Codex stores its local state under `CODEX_HOME` (defaults to `~/.codex`)"
  ([Codex advanced configuration](https://learn.chatgpt.com/docs/config-file/config-advanced),
  fetched 2026-09-17).
- A hook is started by the host and inherits its environment, so inside a hook these
  variables say where this very host keeps its sessions. Trusting them widens nothing an
  attacker could not already reach: whoever sets the host's environment already chooses
  what the hook runs.
- The scheduler's environment is not the host's, so the nightly backfill may not see the
  variable. The default directories therefore stay on the list; the configured ones are
  added to it.

## The decision

- One function, `host_transcripts.host_transcript_roots()`, names the directories: the two
  defaults, plus `$CLAUDE_CONFIG_DIR/projects` and `$CODEX_HOME/sessions` when set. The three
  copies call it.
- Everything else about the allowlist stays: resolved path, extension list, symlink refusal.

Files: `scripts/host_transcripts.py`, `scripts/integration_adapter.py`,
`scripts/flush_memory.py`, `scripts/backfill_sessions.py`,
`tests/test_a_moved_host_directory_is_still_the_host.py`
