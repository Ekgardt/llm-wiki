# Queue task records

This owner converts already-read task/history rows into the existing QueueTask
and AttemptRecord values, and constructs the existing legacy/v3 export objects.
It owns representation and the unchanged queue-task-v3.json resource. It opens no
database, reads no task rows, acquires no lease and starts or commits no transaction.
It has no dependency on the queue executor, controller or Markdown transactions.

The queue retains source ordering, payload decoding/hash validation and the caller's
transaction. Record conversion uses the existing queue_timing timestamp functions
and queue_contracts dataclasses. No field is renamed, normalized differently or
silently dropped. Export preserves the difference between absent lease/result
and a partially populated object; the existing schema rejects malformed v3 records.
Capture links and optional seals retain their current fields and null semantics.

The v3 resource remains scripts/schemas/queue-task-v3.json. The new package location
changes only how that same path is located. Existing formats.schema validates its
closed supported subset. No external schema engine or new runtime format is added.
Four direct aliases in memory_queue remain because actual queue read/export paths
call them; _ExportedRows retains its historical module identity through the alias.
The five internal field helpers have one implementation in this owner. Public task
and attempt dataclass identities remain the original queue_contracts contract.

Tests compare retained pre-transfer outputs from real queued rows, schema failures
and history/null/date behavior. Existing queue export, migration/purge and corruption
consumers remain unchanged integration obligations and run with the new owner tests.
Full source/dependency contracts, schema, fixtures, instructions and measurement tools
belong in the packet. No module-only pass proves whole-queue completion or deployment.
Source: ../research/2026-11-03-queue-task-record-boundary.md.

Shared canonical contract: [Queue value contracts](queue-contracts.md).
Shared canonical contract: [Queue timing](queue-timing.md).
Shared canonical contract: [Closed formats](formats.md).

The normal export and corruption-history paths share `_attempt_history_entry`.
Its five-field dictionary was AST-identical in both former implementations; the
list mapper now calls the same function and the queue retains its required alias.

## Structural reference retained from the overview

### Queue task record boundary

Task/history row conversion and legacy/v3 export representation move to
`llm_wiki.queue_task_records`; SQL reads, payload checks and transactional ownership
remain in `memory_queue`. The existing v3 schema and public record identities stay
unchanged. Full contract: [queue task records](queue-task-records.md);
[research](../research/2026-11-03-queue-task-record-boundary.md).

### Queue evidence representation

Canonical corrupt-export, disposition, capture-resolution, purge and seal records
move to `llm_wiki.queue_evidence_records`; all SQL, file IO, authorization and live
state transitions remain in the queue. Normal and corrupt history reuse the same
single-row formatter in `queue_task_records`. Full contract:
[queue evidence records](queue-evidence-records.md);
[research](../research/2026-11-03-queue-evidence-record-boundary.md).
