# A subagent's report is kept with the session that asked for it

Date: 2026-09-24. Audit items B-12 (third point) and C-15 of
`docs/AUDIT-2026-09-24-live.md`.

## Question

The audit found no `SubagentStop` hook and suspected that subagent sessions are
lost. What of a subagent's work does the memory keep today, and what should it
keep?

## Sources

- Claude Code hooks reference, "SubagentStop" (fetched 2026-09-24,
  https://code.claude.com/docs/en/hooks.md): the event carries the main session's
  `transcript_path`, the subagent's own `agent_transcript_path` in a nested
  `subagents/` folder, and `last_assistant_message`; it also fires for Claude
  Code's internal agents (prompt suggestions, `/btw`), and "To inject context
  into the parent session after a subagent returns, use a PostToolUse hook on the
  Agent tool instead."
- The 2026 ablation the session-evidence decision rests on
  (`scripts/session_evidence.py` docstring): verbatim dialogue beats extracted
  artifacts, and tool output is the noise that drowns it.

## Findings (facts, measured on this machine)

1. 319 of 320 subagent calls in the host's transcripts ran in the background
   (`toolUseResult.status == "async_launched"`). Their reports reach the parent as
   a user-turn `<task-notification>` with a `<result>`, which the session record
   already keeps verbatim and the classifier reads in its tail.
2. A subagent that runs in the foreground returns its report as the `tool_result`
   of the `Agent` call (`Task` in older hosts). `session_evidence.render_transcript`
   drops every `tool_result`, so that report is missing from the retained record,
   although the classifier's raw tail sees it.
3. A subagent's own transcript is mostly tool traffic. Capturing it on
   `SubagentStop` would classify each subagent separately (one model call each;
   this session alone started dozens), would duplicate the report the parent
   already holds, and would also fire for the host's internal agents.
4. C-15: the 80 LSP failure records (40 on 2026-09-13 00h, 20 on 09-14 21h, 20
   on 09-15 00h, all `process_exited` of a `node` server) predate the
   `stderr_tail` field added on 2026-09-17. No server has failed since. The cause
   of that burst cannot be recovered; the nightly retires the records past 14
   days beyond the newest 20.

## Decision (conclusion)

- No `SubagentStop` capture. The report, which is the subagent's conclusion, is
  kept with the session that asked for it: the session record renders the
  `tool_result` of an `Agent`/`Task` call as `**subagent report:**`, bounded to
  8 000 characters, and skips a background launch receipt
  (`toolUseResult.isAsync`), whose report arrives later as a notification the
  record already keeps.
- C-15 needs no code: every new failure carries the server's last words.

## Edited files

- `scripts/session_evidence.py`
- `tests/test_a_subagents_report_is_kept_with_its_session.py`
- `docs/AUDIT-2026-09-24-live.md`, `CHANGELOG.md`

## Uncertainty

Only one foreground subagent result exists on this machine, so the foreground
shape is taken from the hooks reference and the tool-result format, not from a
sample; the test pins the shape it relies on.
