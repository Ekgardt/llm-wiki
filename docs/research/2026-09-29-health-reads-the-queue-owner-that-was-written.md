# Health reads the queue owner that was written

Date: 2026-09-29. A live health check raised `IndexError: No item with that
key` in `_count_one_queue_owner` while a queue owner was present. An empty
ownership table did not exercise this path.

The producer is `MemoryQueue.queue_owner` → `_insert_queue_projection`.
The V3 schema and insert write `owner_token`, `domain_role`, and `process_id`.
Doctor follows the adoption tombstone to the V3 database, but its shared queue
reader still expected the pre-adoption names `token`, `role`, and `pid`.
The exception escaped the ordinary unreadable-database handling. This is a
schema-contract defect, independently reproduced with real adopted stores and
native worker/operator leases; it does not establish the cause of every cold
MCP timeout.

The reader now normalizes the two supported row layouts at its existing
ownership boundary, preserving the process-start identity and expiry used by
liveness checks. It counts the queue domain role, including operators, rather
than a nested parent's canonical role. An incomplete layout is an explicit
read failure. No exception is suppressed, no database is rewritten, and no
deadline or deletion permission is relaxed.

The pre-adoption layout remains necessary: the installer still accepts V2
stores and refuses migration while their owners are active
(`memory_queue._require_unambiguous_v2_owners`). Removing that read contract
would hide the very owner that prevents safe adoption. It can be removed only
when support for those input stores is deliberately retired; it is not a
second active runtime. The V3 reader no longer assumes that legacy layout.

Regression checks cover native worker and operator ownership, release, and
worker projections under compile/doctor/nightly/weekly parents. Both original
native-owner cases fail on the old implementation with the same `IndexError`.
Additional checks retain pre-adoption worker/migration visibility and reject
incomplete projections. Existing runtime-deletion tests remain required.
This is a repair to the current producer/consumer contract, not a new storage
or ownership design.
