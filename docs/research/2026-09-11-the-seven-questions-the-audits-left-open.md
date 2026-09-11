# The seven questions the audits left open

Date: 2026-09-11. Trigger: `docs/REPORT-2026-09-11-audit-and-issues.md`
listed nine questions the two 2026-09-10 audits could not settle; two were
answered on the way (memory Q2 is M1, memory Q4 went with H2). The owner
asked for the rest to be finished. Each answer below is read from the code
or measured; where it shows a defect, the fix is named.

## Operations Q1 — a compile killed before its stamp

`scheduled_nightly._compile_failed_this_pass` counts a failure only when
`last_compile_finished_at` moved and `last_compile_status == "error"`.
`compile_memory` writes `last_compile_status = "running"` and
`last_compile_started_at` when it starts (`_mark_started`) and the finished
stamp only on the paths it handles (`_mark_finished`). A compile killed by
the OOM killer or a signal writes nothing more, the stamp does not move, and
the nightly records `failures=0` for a night whose compile died — the exact
2026-08-22 failure mode the function's own docstring names, one layer down.

Defect. Fix: the nightly also snapshots `last_compile_started_at` before the
compile step; after the compile stopped (`_wait_compile_finished`), a start
stamp that moved while the status is still `running` and no compile is
running means the compile exited without an outcome, and it is counted with
that reason. A compile started by a hook after the wait is running, so it is
not counted.

## Operations Q2 — Windows Task Scheduler and the other security context

`process_liveness._windows_process_state` opens the PID with
`PROCESS_QUERY_LIMITED_INFORMATION`; only errors 87 and 1168 (no such
process) mean dead, and every other failure — access denied (5) across a
security boundary included — is `unknown`, which `pid_alive` treats as
alive. A PID that does not exist fails with 87 whatever the caller's
context. So a process in another context can be waited for, never read as
dead. Settled by the code; no change.

## Operations Q3 — `refresh-all` killed during activation

`GenerationCatalog._move_active_pointer` updates `catalog_state` and inserts
the `activation_history` row inside `_write_transaction` (`BEGIN IMMEDIATE`
… `commit`), on a rollback-journal database with `synchronous=FULL`
(`CLAUDE.md`, Stage 2 contract). A process killed before the commit leaves a
hot journal that the next connection rolls back, so the pointer is either
the old or the new generation (SQLite, "Atomic Commit In SQLite",
https://www.sqlite.org/atomiccommit.html). Settled; no change.

## Operations Q4 — Codex has no prompt or edit capture

Codex supports both events. Its hooks reference, read today
(https://learn.chatgpt.com/docs/hooks, redirected from
developers.openai.com/codex/hooks): `UserPromptSubmit` receives `prompt`,
`turn_id`, `session_id`, `cwd`; `PostToolUse` fires for "Bash, file edits
via apply_patch, MCP tools, local function tools" with `tool_name`,
`tool_input`, `tool_response`. `integrations/codex/hooks.json` registers
neither for capture (its `PostToolUse` is the #24 graph hint on `Bash`), so
Codex sessions leave no prompt or edit breadcrumbs in the daily log, while
Claude sessions do (`user_prompt_capture.py`, `post_tool_capture.py`).
Capture was left out, not missing in Codex. Parity is its own change, with
the ownership rule (`scripts/codex_hook_identity.py`) and the doctor's
runtime-hook check updated in the same step, because that is what broke the
last time a Codex handler was added.

## Operations Q5 — where the plugin helpers live

There never was a `scripts/plugin*.py`. `tests/test_plugin_helpers.py`
tests the OpenCode plugin's Python side, which is
`scripts/integration_adapter.py` (`normalize_event` and the delegate
runner) and `scripts/codex_memory.py`; the plugin itself is
`scripts/llm-wiki-memory-opencode.js`. Settled; no change.

## Memory Q1 — the guardrails precondition rehash

Measured on the live vault, read-only, five runs each:
`snapshot_guardrail_sources_with_content` 47 ms (163 entries),
`snapshot_claim_tree` 72–104 ms (244 entries); `knowledge/notes` is 924 KB.
It is carried by compile (`claim_tree_manifest` precondition) and
`build_guardrails`. At this size it is not a cost worth a change; the
32 MiB bound stays the ceiling. Settled; no change.

## Memory Q3 — two sessions, one record

`session_evidence._safe_component` replaces every run of characters outside
`[A-Za-z0-9_-]` with `-`, cuts at 64 and falls back to `unknown-session`.
Three inputs therefore share a file: every capture without a session id,
ids that differ only in replaced characters, and ids that share 64
characters. The write replaces the file, so the second session's record
silently takes the first one's place. No such file exists on the live vault
today (real ids are UUIDs or `ses_…`), but the path is a real loss path.
Defect. Fix: when the safe name is not the id itself (replaced characters,
truncation, or no id), the name carries the first 12 hex of the SHA-256 of
the raw id (for a missing id, of the record's own body). Ids that are
already safe keep their current name, so existing records keep theirs.

Files: `scripts/scheduled_nightly.py`, `scripts/session_evidence.py`,
`tests/test_scheduled_nightly.py`, `tests/test_session_evidence.py`,
`docs/REPORT-2026-09-11-audit-and-issues.md`.
