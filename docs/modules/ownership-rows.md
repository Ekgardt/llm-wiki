# Fenced ownership rows

`llm_wiki.ownership_rows` owns the existing frozen acquisition row value, row-to-
process decoder, role/scope epoch allocation, owner insertion and exact-fence
projection deletion. Three existing SQL declaration constants move with them.

It uses a supplied connection and never opens, closes, begins, commits or rolls
back it. The registry retains clocks, role/process/lease validation, admission,
proof that an expired owner is dead and authority to reclaim. Calling a row helper
alone is not proof of ownership or permission to reclaim.

Epoch allocation, insert conflict translation, marker bytes and fixed-width times
retain their original behavior. Cleanup removes only projections matching the
complete role/scope/actor/token/epoch tuple. Capture bindings are removed before
their exact intent fence; other bindings and owners are retained. No schema,
transaction, timeout or retry policy changes.

Original helpers and constants remain identical aliases in operational_ownership;
serialized callable/value names are retained until a qualified consumer migration.
There is one implementation. Complete value/time/schema sources and actual DDL
fixtures are included. Tests first ran on the original implementation and cover
foreign keys, each stale fence component, rollback, transaction-control rejection
and actual SQLite ignored-insert outcomes. Registry/quiescence integration remains
required and is not replaced with mocks.

This packet alone does not qualify the whole registry. See
[research](../research/2026-11-03-ownership-rows-boundary.md).
