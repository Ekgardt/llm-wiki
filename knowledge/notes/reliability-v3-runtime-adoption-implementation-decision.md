---
type: decision
title: "Reliability V3 Runtime Adoption Is Approved For Implementation"
description: "The approved Reliability V3 operational database pair may now be implemented with explicit offline adoption, retained v2 evidence, immutable tombstones, and no change to Markdown authority or runtime roots."
date: 2026-08-12
confidence: high
source_authority: user
status: active
---
# Reliability V3 Runtime Adoption Is Approved For Implementation

One-sentence summary: The approved Reliability V3 operational database pair may now be implemented with explicit offline adoption, retained v2 evidence, immutable tombstones, and no change to Markdown authority or runtime roots.

## Decision

Date: 2026-08-12.

The user explicitly approved implementation of the operational runtime migration
required by the installer repair path. This activates the already reviewed target in
[[knowledge/notes/v4-reliability-contracts-decision]] without changing its architecture.

Implementation will:

- add versioned queue and coordinator SQLite candidates under the existing `run/` root;
- keep SQLite in rollback-journal `DELETE` mode with `synchronous=FULL`, explicit
  transactions, foreign keys enabled, and trusted schema disabled;
- initialize only a provably fresh vault automatically;
- require an explicit offline confirmation before adopting existing v2 databases;
- retain byte-identical v2 databases as migration evidence and replace legacy active
  paths with verified JSON tombstones;
- fail closed during partial or conflicting adoption and require explicit repair to
  resume;
- keep Markdown, Git, project journals, and accepted artifacts authoritative;
- add no daemon, remote service, runtime root, MCP tool, or automatic Git operation.

## Rationale

Installer repair cannot truthfully inspect or repair installed vaults until one shared
backend can distinguish fresh, upgrade-required, partial, adopted, and conflicting
runtime states. Implementing that backend together with the versioned database pair
avoids a second migration implementation and prevents normal startup from guessing
that all agents are stopped.

SQLite documents that `DELETE` is the default rollback-journal mode and that `WAL` is
persistent across connections. Its PRAGMA interface can silently ignore unknown names,
so implementation must read every required setting back. Python documents that
`sqlite3.Connection` must be explicitly closed and that transaction defaults are
evolving; the implementation therefore uses explicit transaction boundaries and
caller-owned close blocks compatible with Python 3.10 through 3.14.

## Consequences

- Existing installed vaults remain unchanged until the operator explicitly selects
  adoption and confirms all agents are stopped.
- A partial migration blocks v3 mutation but preserves all evidence needed to resume.
- Retained v2 database bytes remain protected operational evidence.
- Public repair documentation can be added only after the shared backend and CLI pass
  their behavioral gates.

## Minimal Recovery Record Shapes

The user explicitly approved filling the specification's field-name gaps with the
smallest closed records that can prove the already approved crash invariants. The
committed schemas under `scripts/schemas/` are the exact wire contracts.

- Queue exports contain the complete task state, bounded attempt history, sorted
  source links, and optional capture binding.
- Compile receipts contain the path-bound source and batch manifest, deterministic
  packing/provider budget, one disposition per source, committed operations, and
  source-specific evidence.
- Transaction abort records contain transaction/intent identity, only the SHA-256 of
  the secret fence token, fence epoch, before-manifest hash, restored target count and
  tree hash, actor identity, and chosen abort time.
- Capture-link resolutions contain task identity, superseded digest, three observed
  evidence digests, an optional selected intent descriptor, actor, reason, and chosen
  time.
- Corrupt export, disposition, supersession, and purge records contain operation and
  package identity plus aggregate hashes, generations, counts, roots, actor/reason
  authority where applicable, and no per-page arrays.
- Migration records contain one deterministic operation ID, fresh/upgrade source
  descriptors for the queue/coordinator pair, schema digests, and installed
  integration digest.
- Adoption records contain the migration reference, exact active/tombstone/retired
  artifacts for the pair, database IDs and PRAGMA readback, schema digests, and
  installed integration digest. Fresh records forbid retired artifacts; upgrade
  records require them.

No recovery record stores a secret fence token, arbitrary diagnostic payload, or a
duplicated page ledger. Ordering, cross-record membership, byte ceilings, and live
filesystem identity remain production validation responsibilities because JSON Schema
alone cannot prove them.

## Coordinator Candidate DDL Clarifications

The coordinator plan names projection columns but omits their exact declarations and
mentions trigger/index/schema-marker checks without defining new trigger bodies or a
separate marker table. The implementation uses these minimal clarifications:

- canonical role/scope/actor/token/start-identity columns are bounded `TEXT`; process
  IDs and fencing epochs are positive `INTEGER` values;
- domain projection rows reference a canonical owner through the complete role, scope,
  actor, token, and epoch identity where the approved plan requires that link;
- primary and unique constraints provide the required coordinator indexes; no
  implementation-specific named index is authority;
- the coordinator v3 trigger set is empty because the approved plan defines no
  coordinator trigger name, transition, or body; inventing one would add behavior;
- the exact `application_id=0x4C575433` and `user_version=3`, published only after the
  complete schema invariant, are the schema marker; no extra marker table is added;
- historical nonempty project, writer, or maintenance owner rows are adoption blockers
  because v2 cannot prove canonical role, scope, process-start identity, or liveness;
- historical checkpoint attempts must already be complete and consistent; the v3
  candidate does not invent retry timestamps or ownership evidence.

## Source / Evidence

- Explicit user approval in the 2026-08-12 OpenCode session.
- `docs/superpowers/plans/2026-08-05-v4-reliability-queue.md`, Tasks 1-5.
- `docs/superpowers/plans/2026-08-05-v4-reliability-installer.md`, Task 6.
- SQLite PRAGMA documentation: https://www.sqlite.org/pragma.html
- Python 3.14 `sqlite3` documentation: https://docs.python.org/3.14/library/sqlite3.html

## Related

- [[knowledge/notes/audit-closure-security-recovery-control-plane-decision]]
- [[knowledge/notes/v4-reliability-contracts-decision]]
- [[knowledge/notes/reliable-memory-stage-2]]
- [[knowledge/notes/blackboard-fenced-resource-claims-decision]]
- [[knowledge/notes/durable-capture-producer-activation-decision]]
- [[knowledge/notes/install-ownership-control-plane-decision]]
