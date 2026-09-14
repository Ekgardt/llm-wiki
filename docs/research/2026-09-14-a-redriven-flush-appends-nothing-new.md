# A redriven flush appends nothing new

Dated 2026-09-14. Item 2.12 of `docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- `memory_queue._manual_flush` summarises a session and appends the block with
  `_append_flush_block(..., task_id=str(task["id"]), ...)`, which calls
  `daily_log_append.locked_append_once(daily_path, block, task_id)`: the task id is the
  operation id, and so the marker that makes the append happen once.
- A redrive copies a dead task into a new task with a new id and the same payload
  (`_insert_redriven_task`, `redrive_of` set). A flush task that appended its block and
  then died — the block written, the completion not recorded — is appended again under
  the copy's id when redriven. Found by reading the code.
- The payload names the session, event, day and prompt; two different flushes never
  share it, and a redrive copy always does (same bytes, same `input_hash`).
- The code graph: `_append_flush_block` ← `_manual_flush` ← `_manual_processor`
  (`_MANUAL_TASK_HANDLERS["flush"]`) ← `run_worker` via `memory_queue work`.
  `tests/test_memory_queue.py::test_manual_flush_handler_is_preserved` pins the
  operation id to the task id.

## Practice on this date

- The idempotency key of an effect is derived from what the effect is about, not from
  the delivery that carries it, so that every redelivery — including one an operator
  re-queues under a new message id — maps to the same key
  ([microservices.io, Idempotent Consumer](https://microservices.io/patterns/communication-style/idempotent-consumer.html);
  [AWS Builders' Library, making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)).

## The decision

- The flush block's operation id is `flush:` followed by the SHA-256 of the payload's
  canonical JSON, so a redrive copy recognises the block the original wrote.
- A task that appended under the old task-id marker before this change and is redriven
  after it appends once more; there is no way to map the old marker back to a payload.
- The pinning test's expectation moves to the payload-derived id.

Files: `scripts/memory_queue.py`, `tests/test_memory_queue.py`,
`tests/test_a_redriven_flush_appends_nothing_new.py`,
`docs/research/2026-09-14-a-redriven-flush-appends-nothing-new.md`.
