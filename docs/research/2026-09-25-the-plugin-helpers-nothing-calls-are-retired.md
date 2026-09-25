# The plugin helpers nothing calls are retired

Date: 2026-09-25. Audit item C-7, first part (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and history)

- `integrations/README.md` says the OpenCode plugin "is installed from outside
  this repository ... this directory holds no copy of it" and that it calls
  `scripts/daily_log_append.py` and `scripts/tool_breadcrumb_append.py`.
- The plugin is in the repository (`scripts/llm-wiki-memory-opencode.js`) and
  the installer copies it from there (`integration_hook_config.py`). It calls
  only `integration_adapter.py`. `git log -S` shows it stopped calling both
  helpers on 2026-07-13 (`eb05bac3`); any plugin installed since then does not
  run them, and each install replaces the plugin.
- `tool_breadcrumb_append.py` is imported by nothing but tests.
  `daily_log_append.py` is a live module (`locked_append`, `append_daily`, ...
  used by the capture hooks, the queue, the MCP server, episode consolidation);
  only its command line — `main`, `_stdin_payload`, `_append_event`,
  `_event_operation_id` — is reached by nothing (reachability pass over the
  module, from every name another product module uses).

## Source

- Google SRE book, "Simplicity", https://sre.google/sre-book/simplicity/
  (fetched 2026-09-25): unused code kept "is a metaphorical time bomb waiting to
  explode"; "every line of code ... creates the potential for introducing new
  defects and bugs".

## Decision

- `scripts/tool_breadcrumb_append.py` and the command line of
  `scripts/daily_log_append.py` are removed with the tests that only ran them.
  The appenders stay.
- `integrations/README.md` says what is true: the plugin ships in `scripts/`, the
  installer places it, and it forwards every event to the adapter.
- Uncertain: a plugin installed before 2026-07-13 and never reinstalled would
  lose its breadcrumbs. Such an install also predates the v4 queue, which
  `doctor` already refuses, so it cannot be running today's code.

## Files

- `scripts/tool_breadcrumb_append.py` (removed)
- `scripts/daily_log_append.py`
- `integrations/README.md`
- tests that ran only the removed code
- `CHANGELOG.md`
