# A memory call leaves no session behind, and carries no tools

Dated 2026-09-14. Items 1.2 and 5.2 of `docs/AUDIT-2026-09-14-2.md`. The research
before the fix.

## What was found

- `llm_client._claude_command` runs `claude -p --output-format text`, adding
  `--system-prompt` and `--setting-sources ""` when the CLI has them. It has no
  `--no-session-persistence`, so every memory call — compile drafts, classification,
  contradiction checks, answers, all carrying private vault text — is saved by the CLI
  as a session under `~/.claude/projects/-tmp-llm-wiki-provider-*/`. Counted on this
  machine today: **11 547** such directories. They sit outside the vault's retention,
  deletion and backup rules.
- `backfill_sessions.DEFAULT_SOURCE_ROOTS` scans all of `~/.claude/projects` with no
  filter, so a backfill would turn those provider calls into session records under
  `knowledge/raw/sessions/`.
- Measured with the installed CLI (2.1.270), one trivial prompt from a
  `llm-wiki-provider-` temporary directory, on 2026-09-14:
  - current flags: a new `-tmp-llm-wiki-provider-AjInij` project directory appeared
    (removed afterwards); with `--no-session-persistence` none appeared (count stayed
    11 547);
  - `--output-format json` usage, paired: current flags **3 951 cache-creation +
    9 740 cache-read input tokens**; with `--tools "" --strict-mcp-config` added,
    **750 cache-creation and 0 cache-read** — the built-in tool descriptions are about
    13 000 input tokens on every call. Wall time did not differ for this prompt
    (5.3 s against 5.7 s; 7.7 s against 7.8 s in the text-mode pair).
- The memory calls use no tools: the material is in the prompt and the reply is text
  or JSON (`provider_cwd`'s docstring; no `--allowed-tools` or `--mcp-config` anywhere
  in `llm_client.py`).
- The code graph: `_claude_command` has one caller, `_call_claude`. Tests pin its
  flags in `tests/test_llm_descriptors.py`.
- Codex is not installed on this machine; whether `codex exec` persists a session
  could not be checked. Its session files live under `~/.codex/sessions`, which the
  backfill also scans.

## Practice on this date

- Claude Code CLI reference: `--no-session-persistence` "disable session persistence
  — sessions will not be saved to disk and cannot be resumed (only works with
  --print)"; `--tools ""` disables all built-in tools; `--strict-mcp-config` uses only
  the MCP servers given by `--mcp-config` (the installed CLI's `--help`, 2.1.270;
  [CLI reference](https://code.claude.com/docs/en/cli-reference)).
- Data minimisation: a copy kept outside the system that governs it is a copy its
  deletion rules do not reach (GDPR Art. 5(1)(c), (e) — the principle, not a legal
  claim here).

## The decision

- `_claude_command` adds `--no-session-persistence`, `--tools ""` and
  `--strict-mcp-config`, each only when this CLI's `--help` lists it, like the two
  flags before them.
- `backfill_sessions` skips a transcript that belongs to a memory call: a Claude
  project directory named for a `llm-wiki-provider-` working directory, or a
  transcript whose first recorded working directory is one. The second covers Codex
  and a moved Claude directory without reading past the first records.
- The 11 547 existing directories are the owner's private files outside the
  repository; deleting them is not done automatically. They are reported with the
  command that removes them.

Why not the alternatives:

- **Delete the directories after each call.** Races the CLI's own writes and still
  writes the private text to disk first.
- **Keep tools, add only the persistence flag.** 13 000 input tokens a call for tools
  the call cannot use (rule 4).

Files: `scripts/llm_client.py`, `scripts/backfill_sessions.py`,
`tests/test_llm_descriptors.py`, `tests/test_a_memory_call_leaves_no_session.py`,
`docs/research/2026-09-14-a-memory-call-leaves-no-session.md`.
