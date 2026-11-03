# Runtime metadata validation

`llm_wiki.runtime_metadata` owns seven existing helpers for parent containment,
regular-file/reparse checks, optional size budgets, owner-only admission and
metadata-only operational database validation. Algorithms and errors are unchanged.

It opens/closes no second descriptor and reads no file contents. This matters for
process-associated POSIX SQLite locks, which can be released by an unrelated close
on the same file. Actual database opening, identity bracketing and journal recovery
remain in SQLite IO. Byte reads and platform file identity capture remain in
runtime_records; Windows workspace implementation remains owned by its packet.

SQLite IO now imports the metadata validator directly. Runtime_records and the
reliable_memory facade retain identical aliases and historical serialized names
until a qualified consumer migration; there is one implementation. Metadata checks
do not independently establish stable descriptor identity or authorize a mutation.

The complete context includes runtime-environment permission/platform providers.
The original no-descriptor test moved unchanged; containment, file kind and budget
tests first ran against the old implementation. Real two-process SQLite exclusion
and stranded-journal integrations remain required outside this leaf packet.

See [research](../research/2026-11-03-runtime-metadata-boundary.md).
