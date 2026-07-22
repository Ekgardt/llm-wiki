---
type: decision
title: "Derived Evidence Uses Immutable Cache Generations"
description: "Evidence retrieval uses disposable immutable cache generations while Markdown, Git, and project journals remain authoritative."
date: 2026-07-17
confidence: high
source_authority: user
status: active
---
# Derived Evidence Uses Immutable Cache Generations

One-sentence summary: Evidence retrieval uses disposable immutable cache generations while Markdown, Git, and project journals remain authoritative.

## Decision

Date: 2026-07-17.

Markdown, Git, and project journals are the authoritative product record. All
graph, FTS, vector, tier, and telemetry generation state is disposable and
derived. It must be rebuildable from authoritative sources and must never become
the only copy of knowledge or project history.

`cache/evidence-graph/` is the new target generation layout. A rollback-journal
SQLite catalog selects one active generation. Each generation is immutable after
activation, and optional vectors must be absent, complete, or explicitly stale.
Generation databases do not belong under `run/`, which remains operational state
only.

Legacy `cache/index.sqlite`, `cache/vectors.npy`, `cache/vectors_meta.json`, and
`cache/lancedb/` remain readable during migration. They are disposable derived
caches, not members of a generation. They must not be removed until installed-vault
migration evidence makes that safe.

This decision and its rationale are immutable. A later architecture change must
supersede this page with a new decision rather than rewriting the choice recorded
here.

## Rationale

One validated active-generation pointer prevents readers from combining graph,
search, vector, tier, or telemetry artifacts built from different source
snapshots. Immutable generations also make interrupted builds disposable: an
incomplete generation is never activated, while the previous validated
generation remains selected.

SQLite rollback journals provide atomic commit and crash recovery when the
filesystem honors SQLite's locking and flush assumptions. Therefore these local
databases use rollback-journal mode, `synchronous=FULL`, and no WAL, and require a
local filesystem with correct locking.

## Rejected alternatives

- Treating graph, FTS, vectors, tiers, or telemetry as authoritative was rejected
  because derived state can be stale, incomplete, or deleted.
- Updating one mutable set of retrieval files in place was rejected because
  readers could observe artifacts from mixed source generations.
- Storing generation databases under `run/` was rejected because `run/` has a
  separate operational-state and deletion contract.
- A persistent daemon, remote database, or cloud service was rejected. No persistent daemon
  is required to build, activate, read, or regenerate evidence.

## Consequences

`cache/evidence-graph/` may be deleted and regenerated from Markdown, Git, and
project journals. Deleting it loses only derived retrieval state. A catalog may
select only one validated active generation, activated generations are not
modified, and partial vector state is never silently used.

The existing `run/` deletion contract is unchanged. No generation database
belongs under `run/`; operational transactions, queue work, leases, locks, and
undo artifacts retain their existing protections.

## Source / Evidence

- Canonical plan: `docs/superpowers/plans/2026-07-16-unified-evidence-retrieval.md`,
  Target Runtime Layout (lines 87-107) and Task 5 (lines 388-408).
- SQLite atomic commit and rollback journals:
  https://www.sqlite.org/atomiccommit.html
- SQLite rollback-mode locking and local-filesystem cautions:
  https://www.sqlite.org/lockingv3.html

## Related

- [[read-only-lsp-navigation-engine-decision]]
- [[persistent-code-intelligence-kernel-decision]]
- [[solo-operator-superset-product-decision]]
