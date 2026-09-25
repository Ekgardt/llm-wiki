# A Codex session is read as a conversation

Date: 2026-09-25. Audit item A-10 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `session_evidence.render_transcript` understands one transcript shape: lines
  whose `type` is `user` or `assistant`, with `message.content` blocks. It is
  the text of the session record (`knowledge/raw/sessions/`) and of the capture
  classifier's prompt (`flush_memory.py`, which calls it on the evidence), and
  `backfill_sessions` counts a session empty when it renders nothing.
- A Codex rollout line is `{"timestamp", "type": "response_item", "payload":
  {...}}`. Every line decodes, so the renderer does not fall back to verbatim
  text; none has role `user` or `assistant` at the top, so every line renders to
  nothing. A Codex session leaves no record and gives the classifier no text,
  and no failure says so.
- No Codex rollout exists on this machine (`~/.codex/sessions` is absent), so
  the shape below is taken from the Codex source, not from a live file.

## Source (fetched 2026-09-25 through the GitHub API)

- https://github.com/openai/codex/blob/main/codex-rs/history/src/lib.rs —
  `RolloutLine { timestamp, ordinal?, #[serde(flatten)] item: RolloutItem }`.
- https://github.com/openai/codex/blob/main/codex-rs/history/src/rollout_payload.rs —
  `#[serde(tag = "type", rename_all = "snake_case")]`, `ResponseItem { payload }`
  is written as `"type": "response_item"`.
- https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models.rs —
  `ResponseItem` is tagged by `type`: `message { role, content: [ContentItem] }`
  with `input_text` / `output_text` parts carrying `text`; `function_call { name,
  arguments: String (JSON), call_id }`; `custom_tool_call { name, input }`;
  `local_shell_call { action }`.

## Decision

- Before rendering, a `response_item` line is read as the entry it stands for:
  a `message` from `user` or `assistant` becomes that turn's text, and a
  `function_call`, `custom_tool_call` or `local_shell_call` becomes one tool line,
  named, with its target when its arguments carry one. Other roles (`developer`,
  `system`) and other items (reasoning, outputs, token counts) are not the
  conversation and render nothing, as Claude's non-conversation lines do.
- A command given as a list (`["bash", "-lc", "ls"]`) is shown joined, for both
  hosts.
- The record and the classifier read the same rendering, so both are fixed at
  once. No new host path, file or runtime state.

## Files

- `scripts/session_evidence.py`
- `tests/test_a_codex_session_is_read_as_a_conversation.py`
- `CHANGELOG.md`
