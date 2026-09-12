# Codex leaves breadcrumbs too

Date: 2026-09-11. Trigger: operations audit Q4, answered in
`docs/research/2026-09-11-the-seven-questions-the-audits-left-open.md`:
Codex supports the prompt and tool events, and llm-wiki never registered
capture for them, so a Codex session leaves no mid-session record of what
was asked or edited while a Claude session does.

## The host contract, read today

Codex hooks reference (https://learn.chatgpt.com/docs/hooks, redirected from
developers.openai.com/codex/hooks), read 2026-09-11:

- `UserPromptSubmit` takes no matcher and receives `prompt`, `turn_id`,
  `session_id`, `cwd`. Plain text on stdout "is added as developer
  context"; JSON may carry `hookSpecificOutput.additionalContext` or a
  block decision. So capture must print nothing.
- `PostToolUse` matches on `tool_name` and fires for "Bash, file edits via
  apply_patch, MCP tools, local function tools"; "Bash and apply_patch use
  `tool_input.command`", and the input reports `tool_name: "apply_patch"`
  whatever alias matched. Plain text on stdout is ignored, empty stdout is
  normal; `updatedMCPToolOutput` and `suppressOutput` are parsed but not
  supported.

## What already fits

`integration_adapter.py` accepts `--source codex`; with no `--delegate` an
event goes through `ingest_event`, which for `user_prompt` runs
`user_prompt_capture.py` and `feedback_capture.py` (the same path the
OpenCode plugin uses) and for `post_tool_use` runs `post_tool_capture.py`.
Neither prints: the adapter's output is `None` for every event but
`session_start`, and a delegate's stdout is forwarded only when it is a
`hookSpecificOutput` with `additionalContext`, which the capture delegates
never write. `session_id`, `cwd` and `tool_use_id` are read by the existing
normalization.

## What changes

- `integrations/codex/hooks.json`: `UserPromptSubmit` →
  `integration_adapter.py --source codex --event user_prompt`;
  `PostToolUse` with matcher `^(apply_patch|Bash)$` →
  `integration_adapter.py --source codex --event post_tool_use`, beside the
  existing graph hint on `Bash`. Both with `commandWindows` and a 5 s
  timeout like the Claude capture hooks.
- `integration_adapter._tool_payload`: `apply_patch` is an edit, recorded as
  `Edit`, and its target is the first `*** Add|Update|Delete File: <path>`
  line of the patch rather than the patch text.
- `scripts/codex_hook_identity.py`: the two adapter commands are ours, so the
  installer's ownership merge and the doctor agree (the #24 C2 lesson: a new
  Codex handler the doctor does not know made every installed Codex report
  `runtime_hooks_mismatch`).
- `scripts/doctor.py`: `userPromptSubmit` is a canonical Codex event name.

Files: `integrations/codex/hooks.json`, `scripts/integration_adapter.py`,
`scripts/codex_hook_identity.py`, `scripts/doctor.py`,
`tests/test_codex_capture_hooks.py` (new), `tests/test_codex_rendered_hooks.py`,
`tests/test_codex_hooks_ownership.py`, `integrations/README.md`,
`CHANGELOG.md`.
