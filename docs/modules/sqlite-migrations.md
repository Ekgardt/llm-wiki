# Resumable SQLite statement execution

llm_wiki.sqlite_migrations runs existing MigrationStatement values on a supplied
connection. Unique nonempty names are required before execution. A statement whose
completion callback succeeds is skipped. Otherwise events are before:name,
after_execute:name within BEGIN IMMEDIATE, and after_commit:name after commit.
The statement invariant is checked before commit. The final invariant runs after
all individual commits; its failure does not undo earlier committed statements.

The sqlite_transactions owner preserves rollback and original exception behavior.
A restart skips only statements with a successful current completion invariant.
This owner neither opens connections nor writes a migration ledger, changes schema
policy, adopts runtime state or wraps the whole plan in a new transaction.
reliable_memory keeps identity aliases, including the historical public pickle name.
Unit tests use real SQLite for rollback, committed restart, duplicate-name refusal
and invariant failures. Existing operational-migration tests separately retain
real-file opening and abrupt subprocess-crash coverage at every event boundary.

Run `uv run pytest tests/modules/sqlite_migrations -q -s`.
See ../research/2026-11-03-sqlite-control-boundaries.md.
