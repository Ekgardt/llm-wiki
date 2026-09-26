# A Claude prompt reaches feedback capture

Date: 2026-09-25. Audit item B-7 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `ingest_event` handles a `user_prompt` by running `user_prompt_capture.py` and
  then `feedback_capture.py` (`_ingest_user_prompt`). Codex and OpenCode reach
  it.
- Claude's hooks (`integrations/claude-code/*.json`) pass
  `--event user_prompt --delegate user_prompt_capture.py` and
  `--event post_tool_use --delegate post_tool_capture.py`. `_dispatch_cli_event`
  sends any named delegate that is not a retired capture name
  (`CAPTURE_DELEGATES`) to `_run_own_delegate`, which runs that one script. So a
  Claude prompt was written to the daily log but never reached feedback capture;
  corrections given to Claude were not collected.
- The named delegate is exactly the script `ingest_event` runs for that event, so
  the flag only chose the narrower path by accident.

## Source

- Claude Code hooks, https://code.claude.com/docs/en/hooks (fetched 2026-09-25):
  a `UserPromptSubmit` hook receives the prompt, and plain stdout of exit 0 is
  added as context — the adapter's ingest path forwards `user_prompt_capture`'s
  stdout the same way (`forward_stdout=True`), so taking it changes nothing the
  host sees.

## Decision

- One table names, per event, the delegates `ingest_event` itself runs:
  `user_prompt` → `user_prompt_capture.py`, `post_tool_use` →
  `post_tool_capture.py`, and the two retired names kept for old settings
  (`pre_compact` → `precompact_capture.py`, `session_end` →
  `session_end_capture.py`). A named delegate from that table takes the full
  ingest path; any other delegate still runs on its own. Installed settings need
  no change.

## Files

- `scripts/integration_adapter.py`
- `tests/test_a_claude_prompt_reaches_feedback_capture.py`
- `CHANGELOG.md`
