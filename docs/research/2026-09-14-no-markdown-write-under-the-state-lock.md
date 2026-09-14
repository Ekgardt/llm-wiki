# No Markdown write under the state lock

Dated 2026-09-14. A guess at the end of `docs/AUDIT-2026-09-14-2.md`, confirmed by a
read-only audit today. The research before the fix.

## What was found

- `memory_state.update_state` holds the `run/state.json` lock while its mutator runs.
- One mutator writes Markdown: `flush_memory._persist_flush` →
  `_append_flush_state`, which checks the dedupe window (`should_skip`), calls
  `append_daily` (a transactional append through `markdown_transaction`, bounded by the
  Markdown writer gate, not by the state lock's caller), then `record_flush` and the tier
  counts. Every other mutator the audit read changes state in memory only.
- Hooks wait for that lock briefly: `HOOK_STATE_LOCK_TIMEOUT = 0.1` s in
  `user_prompt_capture`, 0.1 s in `session_start_context`, 0.5 s in `capture_diagnostics`
  and `integration_adapter`. While a flush waits on a busy Markdown writer with the state
  lock held, those hooks get `StateLockTimeout`: prompt counters and dedupe claims are
  lost, and project checkpoint enqueues fail and are retried.
- The lock around the append exists for one reason: two flushes of the same session and
  event must not both append. `append_daily` is idempotent only when the flush carries a
  `source_event_id`.
- The code graph: `_persist_flush` ← `_settle_flush` ← `_run_flush` (the detached flush
  path); `should_skip`/`record_flush` are also used by `_run_flush` and
  `_record_empty_state`.

## Practice on this date

- Hold a lock only for the state it protects, never across I/O to another resource with
  its own lock; claim, act, then record (the pattern `_drain_project_checkpoint_once`
  already uses in this codebase for the project journal).

## The decision

- `_persist_flush` becomes three steps. Under the state lock: skip if the dedupe window
  says so, otherwise claim by recording the flush (`record_flush`) — a concurrent flush
  of the same session and event now sees the claim and skips, as it saw the lock before.
  Outside the lock: the daily append. Under the lock again: the tier counts. If the
  append raises, the claim is released under the lock so the flush can be retried.
- A process killed between the claim and the append loses that flush for the dedupe
  window (60 s) and is retried after it, as the detached path already is.

Files: `scripts/flush_memory.py`, `tests/test_no_markdown_write_under_the_state_lock.py`,
`docs/research/2026-09-14-no-markdown-write-under-the-state-lock.md`.
