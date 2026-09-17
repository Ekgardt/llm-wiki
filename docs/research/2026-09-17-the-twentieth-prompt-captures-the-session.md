# The twentieth prompt captures the session

Dated 2026-09-17. Finding C-F1 of the third audit (medium, reproduced). The research before
the fix.

## What was found

- Every twentieth prompt `user_prompt_capture.py` starts a detached `flush_memory.py` with
  `--transcript hook["transcript_path"]`. It is the safety net for a long session that never
  compacts and never ends cleanly.
- Every shipped hook reaches that script through `integration_adapter.py`, and the adapter
  hands a prompt to its delegate as `{"prompt": …}` plus the common fields. The transcript
  path the host sent is dropped twice: `_user_prompt_payload` does not read it and
  `_prompt_capture_fields` does not forward it.
- So the flush runs with `--transcript ""`, reads nothing, classifies nothing as `ok`, and
  adds one to `flush_empty_count` and `flush_tier_counts.ok`. The net has caught nothing
  since the adapter became the only entry, and it inflates the "sessions with nothing worth
  keeping" counter. Reproduced by the audit and again here.
- The script it starts is the retired path: no capture intent, no session record through
  the writer gate, no retry. The adapter's own `pre_compact` route has all three.

## Practice on this date

- The host does send the path with a prompt. Claude Code lists `transcript_path` among the
  fields every hook receives: "Path to conversation JSON. The transcript file is written
  asynchronously and may lag the in-memory conversation, so it may not yet include the
  current turn's most recent messages when a hook fires"
  ([Claude Code hooks reference](https://code.claude.com/docs/en/hooks), fetched 2026-09-17).
  Codex lists the same common field as `transcript_path (string | null)`
  ([Codex hooks](https://learn.chatgpt.com/docs/hooks), fetched 2026-09-17).
- The same page gives the budget: a command hook that reaches its timeout is cancelled and
  its output discarded. The shipped `UserPromptSubmit` hook has 5 seconds, so reading and
  redacting a transcript that can be 100 MB does not belong inside it. The work is handed to
  a detached process, as the old flush was.
- One durable route, not two: a mid-session capture is the same thing a compaction capture
  is — the session so far — so it publishes the same capture intent and wakes the same
  worker. Retry, the session record and the failure trail come with it.

## The decision

- The adapter keeps `transcript_path` on a `user_prompt` event and forwards it to the prompt
  delegate.
- On the twentieth prompt the delegate starts `integration_adapter.py --running-capture
  <payload>` detached. That mode publishes a `pre_compact` capture intent with the trigger
  `prompt-count-20` and wakes the capture worker. It writes no project checkpoint: nothing
  was compacted.
- With no transcript path nothing is started, so an empty flush is no longer counted.
- A failure in that mode is recorded as `adapter_running_capture`, like the other modes.

Files: `scripts/integration_adapter.py`, `scripts/user_prompt_capture.py`,
`tests/test_the_twentieth_prompt_captures_the_session.py`, `tests/test_capture_hooks.py`,
`tests/adopted_capture_vault.py`
