# A tool call in the vault is captured, and a capture hook never runs on a stand-in

Date: 2026-09-25. Audit items B-1 and B-2 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and on the live vault)

- `post_tool_capture._tool_context` drops every tool call whose `cwd` is inside
  the vault. On 2026-09-24 the same rule was removed from the prompt hook
  (`docs/research/2026-09-24-a-long-session-in-the-vault-is-captured.md`): the
  memory's own processes are filtered by the adapter's reentry marker
  (`integration_adapter._is_memory_automation`, `CLAUDE_INVOKED_BY`, applied to
  every host event), which is what "vault-internal sessions are maintenance"
  stood in for. The tool hook kept it, so work done in the vault — which is
  where this product is developed — leaves almost no tool lines since 8 September.
- Both capture hooks still replace a failed import with a silent stand-in:
  `post_tool_capture` for `memory_state` (a no-op `update_state`) and for
  `capture_diagnostics` (a no-op `record_capture_failure`), `user_prompt_capture`
  for `capture_diagnostics`; both fall back from `session_start_project_state` to
  a directory-name slug when the import fails, so a broken install tags lines
  with a different project than `state.md` uses. The hook then runs and writes
  less than it should, and nothing says so.

## Source

- Claude Code hooks, https://code.claude.com/docs/en/hooks (fetched 2026-09-25):
  for `PostToolUse` and `UserPromptSubmit` a non-zero exit other than 2 is "a
  non-blocking error ... the action proceeds, and the transcript shows a `<hook
  name> hook error` notice". A hook that cannot import breaks nothing; it is
  seen. The adapter also records a delegate that exits non-zero
  (`_record_failed_delegate`).

## Decision

- The tool hook captures calls made inside the vault, as the prompt hook does.
- Both hooks import `memory_state`, `capture_diagnostics` and
  `session_start_project_state` plainly. A missing module fails the process,
  which the host shows and the adapter records. Only a path the operating
  system cannot resolve still falls back to the directory name for the slug.

## Files

- `scripts/post_tool_capture.py`
- `scripts/user_prompt_capture.py`
- `tests/test_a_tool_call_in_the_vault_is_captured.py`
- `CHANGELOG.md`
