# Coordinator row and integrity invariants

`llm_wiki.coordinator_invariants` owns eight existing read-only helpers operating
on a caller-owned SQLite connection. It checks SQLite integrity and foreign keys,
operation shape, owner projection identity, fencing epochs, capture bindings and
blackboard claim consistency; it also reports per-table row counts and constructs
the existing coded database-contract errors.

SQLite integrity_check does not replace foreign_key_check. Both run in their
original order. Operation and cross-table checks retain the same SQL, including
process-start identity, complete owner fencing tuples, exact capture-fence binding,
nondecreasing owner/intent epochs, exact blackboard epochs and coherent claim sets.
Error classes, codes, messages, return formats and short-circuit behavior remain.

The module opens/closes no connection, performs no DML and begins/commits/rolls
back no transaction. The caller retains file containment, owner-only admission,
connection/header settings, schema validation, deadlines and lifecycle. Migration,
publication, repair and recovery remain in markdown_transaction. No new state owner,
SQL schema, permission, retry, timeout or runtime path is introduced.

The old module re-exports identical helper objects with historical serialized
identities. They remain until consumers and serialized paths can migrate under a
compatible contract; there is one implementation. The schema and SQLite-contract
owners remain independent dependencies with full source and guides in this packet.

`coordinator-invariants-context.json` contains all own tests, data, instructions
and required source. Tests first ran against the original code, then its exact
transfer: actual SQLite DDL, valid and deliberately inconsistent rows, the two
unchanged index/foreign-key corruption regressions and an authorizer that denies
writes/transaction control. Original adoption/migration/ownership/blackboard tests
remain explicit integration obligations and are run without weakened assertions.
This packet alone does not qualify the coordinator's larger composition closure.

See [research](../research/2026-11-03-coordinator-invariants-boundary.md).
