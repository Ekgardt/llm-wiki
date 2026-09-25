# A hook stops its delegate before the host stops the hook

Date: 2026-09-25. Audit item B-11 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and measured on this machine)

- The Claude hooks this repository installs (`integrations/claude-code/settings.json`)
  give `UserPromptSubmit` and `PostToolUse` 5 seconds. The adapter gave every
  delegate `DELEGATE_TIMEOUT_SECONDS` = 10. A delegate that hangs is killed by
  the host first, and with it the adapter, before `_record_failed_delegate` or
  the timeout handler can record the loss.
- Since B-7 (same day) a Claude prompt runs two delegates in turn,
  `user_prompt_capture.py` and `feedback_capture.py`.
- Measured with `/usr/bin/time`, three runs each, fake provider: the whole
  prompt hook as the host runs it (`uv run ... integration_adapter.py --source
  claude --event user_prompt --delegate user_prompt_capture.py`) took 0.32–0.49 s;
  `user_prompt_capture.py` alone 0.10–0.28 s, `feedback_capture.py` 0.17–0.18 s.

## Source

- Claude Code hooks, https://code.claude.com/docs/en/hooks (fetched 2026-09-25):
  each hook command carries its own `timeout`, and a hook that is stopped is a
  non-blocking error — the host moves on; nothing inside the hook gets to report.

## Decision

- Delegates on the 5-second events get their own bound: `user_prompt_capture.py`
  2.5 s, `feedback_capture.py` 1 s, `post_tool_capture.py` 3.5 s — about ten
  times the measured cost, and with at least 1 s left for the adapter's own start
  under the host's limit. Every other delegate keeps 10 s, under 15-second hooks.
- A test reads the installed hook timeouts from `settings.json` and refuses any
  event whose delegates' bounds plus 1 s exceed it, so the two numbers cannot
  drift apart again.

## Files

- `scripts/integration_adapter.py`
- `tests/test_a_claude_prompt_reaches_feedback_capture.py`
- `CHANGELOG.md`
