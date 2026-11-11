# Compile journal schema

`llm_wiki.compile_journal_schema` owns the unchanged journal version (4), exact
three CREATE statements and two protected-object validators. The table stores
append-only work events, keyed by work/sequence, with a nonnegative sequence and
a task foreign key. UPDATE and DELETE triggers retain their original refusal
messages. The module neither opens a database nor creates or migrates a schema.

Validation reads sqlite_master through a caller-owned connection with sqlite3.Row
rows. Object type and stored SQL must equal the existing expected values (the
CREATE text is stripped as before). Missing or changed objects raise the same
ProgressCorrupt class. It validates only these three protected objects, not the
entire queue inventory or event semantics. Callers retain transaction/lifetime
ownership; storage and explicit migration decide when statements execute.

compile_progress re-exports the same version, tuple and functions for existing
consumers. Those aliases remain until a separately approved compatibility
migration; no schema/hash/version change is part of this extraction. Unit tests
use real SQLite, original SQL fixtures, protected-trigger mutations and unchanged
constraint failures. Full storage, migration and queue integration remain required.

Shared canonical contract: [Compile event records](compile-event-records.md).
Source: [database boundary research](../research/2026-11-03-queue-database-contract-boundary.md).
