# Queue evidence records

This owner constructs immutable corrupt-export/disposition, capture-resolution,
purge and seal records and checks their exact identity fields. Its frozen evidence
value carries already-captured bytes/digests. It grants no lease, authority, consent
or deletion permission; it opens no database and writes no file. Actual operations,
SQL ordering, fencing and authorization remain with the queue.

Canonical bytes and SHA-256 use the existing formats owner. Schema checks use the
existing closed validator and the unchanged four scripts/schemas resources named
in the manifest. The new module's package-parent path points to those same files.
No field, null convention, timestamp, digest input or public format changes.

corrupt_records_match checks hashes and task/package identity against the supplied
row. It does not certify directory permissions, schema or raw artifact contents;
those separate caller checks remain mandatory. require_identical_seal accepts only
the same active digest, consumer kind/id and seal digest, otherwise preserving the
existing capture_link_sealed refusal. Constructing a record never persists it.

The root queue retains direct aliases for all ten moved definitions because its
methods call them or use the value type. CorruptEvidence retains its memory_queue
module identity. No second implementation remains. Aliases retire only when their
actual queue/serialization consumers no longer need them.

Run owner tests with existing capture-link, corruption/purge, queue and migration
integration tests. Preserve original record/digest fixtures, schema bytes and all
assertions. Check Python 3.14 and native platform limitations explicitly. A passing
format packet does not establish complete queue isolation, authorized disposal or
useful full-cycle token savings. Source: ../research/2026-11-03-queue-evidence-record-boundary.md.

Shared canonical contract: [Canonical and schema formats](formats.md).
Shared canonical contract: [Queue contracts](queue-contracts.md).
Shared canonical contract: [Queue timing](queue-timing.md).

## Structural reference retained from the overview

### Queue evidence representation

Canonical corrupt-export, disposition, capture-resolution, purge and seal records
move to `llm_wiki.queue_evidence_records`; all SQL, file IO, authorization and live
state transitions remain in the queue. Normal and corrupt history reuse the same
single-row formatter in `queue_task_records`. Full contract:
[queue evidence records](queue-evidence-records.md);
[research](../research/2026-11-03-queue-evidence-record-boundary.md).
