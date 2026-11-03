# Ownership rows inside the caller's transaction

Date: 2026-11-03. Internal boundary under the approved whole-project mandate.

Transfer _AcquireRequest, _row_process, _next_owner_epoch, _insert_owner_row,
_owner_key and _delete_owner_projections, with the three existing projection SQL
constants, exactly into llm_wiki/ownership_rows.py. Old aliases and serialized
identities remain. The helper uses a supplied connection; it does not open, close,
begin, commit or roll back it. Clock reads, role/lease/epoch validation, admission,
proof of process death/lease expiry and all state authority stay with the registry.

Preserve every SQL statement, field order, marker representation, error code and
row-count check. Epoch increments stay scoped by role/scope and roll back with the
caller. Projection deletion uses the full five-part canonical owner key and removes
only a capture binding joined to that exact intent fence before deleting the fence.
Do not substitute role-only deletion, cascade rules, independent transactions or
a second registry. These would change safety/atomicity, not just module structure.

Fresh builtin37248b32 generation-18e491815827dbc2-50415bf2; architecture and seven
symbol caller queries used no fallback. Direct inspection establishes constructor
use omitted by the partial graph and found no patches of the selected helpers.
Baseline73passed. Before transfer, use actual DDL and foreign keys for epoch scope,
rollback, row conflict translation, each mismatched fencing-key component and
binding cleanup. Forbid helper transaction control with a SQLite authorizer.
Preserve every original definition and SQL constant AST. Measure CCN and full
context including schema/test fixtures and time/value providers. Keep actual
registry, backup and quiescence integrations; mocks alone do not qualify this seam.

Primary sources checked2026-11-03 (three independent publishers):
- SQLite https://www.sqlite.org/foreignkeys.html and
  https://www.sqlite.org/lang_returning.html — compound references, deletion order
  and returned directly modified rows; these two pages count as one publisher.
- Python https://docs.python.org/3.14/library/sqlite3.html — caller-owned connection
  and explicit transaction boundaries must remain unchanged.
- Fowler https://martinfowler.com/bliki/BoundedContext.html — explicit responsibilities
  and relationships support this split; they do not prove SQL invariants.

No schema migration, source-closure exception, new limit or live installation.
This packet does not by itself qualify the complete ownership registry.
