# A dead task names its reason

Dated 2026-09-23. Closes C3 of `docs/AUDIT-2026-09-23-live.md`.

Files: `scripts/memory_queue.py`, `scripts/flush_memory.py`,
`tests/test_a_dead_task_names_its_reason.py`,
`tests/test_capture_worker_fails_instead_of_abandoning.py`,
`tests/test_an_absent_provider_is_waited_for.py`,
`docs/research/2026-09-23-a-dead-task-names-its-reason.md`.

## What was found

- On the live vault 25 `flush` tasks are dead after 8 attempts each, and every one of
  their 225 failed attempts carries the same code, `processor_failed`. The worker maps
  every exception a processor raises to that one code (`_run_processor` returns a bare
  `False`; `flush_memory._capture_queue_failure` names only the absent provider).
- The reason did exist, elsewhere and for a few: `logs/capture-failures.jsonl` holds
  `adapter_capture_worker deferred: QueueOperationError: intent_fence_lost` at the exact
  finish times of the newest failed attempts (2026-09-11 23:04:10, 2026-09-12 15:34:45),
  seven such rows between 2026-09-07 and 2026-09-12. The trail has 70 worker rows for
  225 failed attempts, so most attempts left no reason anywhere.
- `attempt_history.error_code` is bounded to 64 bytes by the v3 schema's CHECK and is
  immutable; `doctor` accepts any code up to 200 characters without a line break. A
  `QueueOperationError` already carries a stable code by contract; a `RuntimeError`
  raised by the transaction layer carries a code as its whole message
  (`RuntimeError("intent_fence_lost")`).

## Practice on this date

- A machine-readable error identifier belongs with the failure record, and free text
  does not: RFC 9457 keeps a stable `type` apart from human `detail`
  ([RFC 9457, Problem Details for HTTP APIs](https://www.rfc-editor.org/rfc/rfc9457)).
  Exception messages may carry paths or content; exception types and codes do not.

## The decision

One function, `memory_queue.processor_error_code(error)`, names the code a failed
processor leaves: a queue error's own code; otherwise `processor_failed:<reason>`, where
the reason is the message when the message is itself a code, else the exception's type
name; bounded to the column's 64 bytes. The worker and the capture worker both use it.
No schema change, no new column, no message text stored.

## Cost, by rule 4

One string format per failed attempt. Existing tests that expected the bare code for
an exception now expect the named one.

## Sources

- [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) — fetched 2026-09-23.
- `run/queue-v3.sqlite3` (read-only) and `logs/capture-failures.jsonl` on the live vault, 2026-09-23.
