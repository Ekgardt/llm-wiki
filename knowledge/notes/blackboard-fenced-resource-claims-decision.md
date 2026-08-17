---
type: decision
title: "Blackboard Uses Fenced Resource Claims"
description: "Blackboard coordination uses two bounded tables in the existing coordinator-v3 database for atomic resource claims while immutable coordination history remains authoritative Markdown."
date: 2026-08-15
confidence: high
source_authority: user
status: active
---
# Blackboard Uses Fenced Resource Claims

One-sentence summary: Blackboard coordination uses two bounded tables in the existing coordinator-v3 database for atomic resource claims while immutable coordination history remains authoritative Markdown.

## Decision

Date: 2026-08-15.

The user explicitly approved this coordinator-v3 schema extension after reviewing its
scope and installed-vault impact. It implements the blackboard part of
[[knowledge/notes/audit-closure-security-recovery-control-plane-decision]] without a
new database, runtime path, daemon, or MCP tool.

- `blackboard_claim_epochs` stores the monotonic fencing epoch for each bounded
  `(project, resource)` identity.
- `blackboard_claims` stores only active logical leases: claim/task identity, agent,
  exact lease token, fencing epoch, heartbeat, and expiry.
- One `BEGIN IMMEDIATE` transaction normalizes, checks, and acquires the whole
  resource set. A conflict rejects the whole set; partial ownership is impossible.
- Heartbeat, completion, and release require the exact claim, token, epoch, project,
  and complete resource set. A stale caller cannot mutate a successor.
- Reclaim is allowed only after expiry. A live unexpired logical lease is never
  guessed dead from process state because agent commands may run in different host
  processes.
- Task, completion, conflict, and resolution events remain append-only Markdown JSONL
  under the existing blackboard path. SQLite remains coordination state, not knowledge
  authority.
- Resources use a bounded deterministic NFC/slash/case-folded identity. Empty,
  absolute, traversal, control-character, duplicate, and over-bound resources fail
  before database mutation.

## Migration

The coordinator application ID and Reliability-v3 `user_version=3` remain unchanged;
the exact schema digest changes. Fresh databases contain both new tables. Existing
installed coordinator-v3 databases require the explicit offline re-adoption path:
copy into a candidate, add only the two empty tables under one immediate transaction,
validate exact schema, foreign keys, and integrity, then publish through the existing
adoption boundary. Live, partial, unknown, or schema-conflicting state blocks adoption.

## Rejected Alternatives

- A third operational database was rejected because it would split ownership and
  violate the two-database Reliability-v3 contract.
- Process-bound `maintenance_owners` rows were rejected because blackboard agents may
  heartbeat from later CLI processes and need multi-resource all-or-none acquisition.
- Task-text word overlap was rejected because it is neither a stable resource identity
  nor a pre-mutation exclusion boundary.
- Automatic overwrite after expiry without an incremented epoch was rejected because
  stale holders could mutate a successor.

## Acceptance

Completion requires fresh-schema and offline-upgrade tests, multiprocess same-resource
exclusion, disjoint-resource concurrency, heartbeat/expiry/reclaim, stale-token and
stale-epoch rejection, all-or-none multi-resource claims, immutable conflict and
resolution events, crash-boundary recovery, mandatory complexity checks, and full
coordinator/ownership/runtime-deletion regressions.

## Source / Evidence

- User approval in the 2026-08-15 audit-closure implementation session.
- SQLite transaction semantics: https://www.sqlite.org/lang_transaction.html
- SQLite schema migration guidance: https://www.sqlite.org/lang_altertable.html
- SQLite foreign-key validation: https://www.sqlite.org/foreignkeys.html
- SQLite integrity checks: https://www.sqlite.org/pragma.html#pragma_integrity_check
- `scripts/markdown_transaction.py`
- `scripts/operational_ownership.py`
- `scripts/blackboard.py`

## Related

- [[knowledge/notes/audit-closure-security-recovery-control-plane-decision]]
- [[knowledge/notes/reliability-v3-runtime-adoption-implementation-decision]]
- [[knowledge/notes/v4-reliability-contracts-decision]]
