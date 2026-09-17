# Every hook writer gives up before its host does

Dated 2026-09-17. Finding C-F8 of the third audit (low-medium, confirmed by reading). The
research before the fix.

## What was found

- Three hook delegates append a line to the daily log through the same writer:
  `post_tool_capture`, `user_prompt_capture` and `session_end_project_tag`. The writer retries
  its compare-and-swap until a deadline, and its default deadline is "never".
- On 2026-08 the tool breadcrumb was given a 7 second budget so that under contention it
  records its own reason instead of being killed mid-retry (250 losses with no reason
  before that). The other two were left on "never": the same defect, one instance fixed.
- The 7 seconds were measured against the adapter's own 10 second delegate timeout. The
  shipped hook configuration gives `UserPromptSubmit` and `PostToolUse` 5 seconds
  (`integrations/claude-code/settings.json`, `integrations/codex/hooks.json`), so the host
  cancels the hook before either inner bound can fire and the breadcrumb still vanishes
  without a reason.

## Practice on this date

- "Claude Code cancels a `command`, `http`, or `mcp_tool` hook that reaches its `timeout`,
  discarding the hook's output" ([Claude Code hooks reference](https://code.claude.com/docs/en/hooks),
  fetched 2026-09-17). Codex: "`timeout` is in seconds. If `timeout` is omitted, Codex uses 600
  seconds for most hooks" ([Codex hooks](https://learn.chatgpt.com/docs/hooks), fetched
  2026-09-17) — ours is set, to 5.
- Nested timeouts only work when each inner one is shorter than the one outside it, with room
  left for the inner party to report. The repository already states this rule in
  `docs/research/2026-09-14-every-budget-inside-its-step.md`.

## The decision

- The budgets live beside the writer, in `daily_log_append`: 3 seconds for a prompt or tool
  breadcrumb (host timeout 5, leaving 2 for the interpreter to start and for the failure line)
  and 7 seconds for the session-end tag (host timeout 15, delegate timeout 10).
- All three delegates pass a deadline. A test reads the two shipped hook files and fails if a
  breadcrumb budget no longer fits inside the host's timeout.
- The hook timeouts themselves are not changed; they belong to the installer's files.

Files: `scripts/daily_log_append.py`, `scripts/post_tool_capture.py`,
`scripts/user_prompt_capture.py`, `scripts/session_end_project_tag.py`,
`tests/test_every_hook_writer_gives_up_before_its_host.py`,
`tests/test_the_breadcrumb_gives_up_before_it_is_killed.py`
