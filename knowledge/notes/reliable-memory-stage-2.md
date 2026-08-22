---
type: decision
title: "Reliable Memory Uses Recoverable Markdown Transactions And Derived Operational State"
description: "Stage 2 keeps Markdown authoritative while adding recoverable transactions, durable checkpoints, safe archives, versioned compile caching, a fenced priority queue, and evidence-backed claims."
timestamp: 2026-07-13T00:00:00
confidence: high
source_authority: ai-derived
status: active
---
# Reliable Memory Uses Recoverable Markdown Transactions And Derived Operational State

One-sentence summary: Stage 2 keeps Markdown authoritative while adding recoverable transactions, durable checkpoints, safe archives, versioned compile caching, a fenced priority queue, and evidence-backed claims.

## Decision

Date: 2026-07-13.

The approved second roadmap stage implements six reliability boundaries:
recoverable multi-file Markdown transactions, project checkpoints independent of
SessionEnd, manifest-verified daily archives, content-addressed compile plans,
priority/lease/dead-letter queue semantics, and claim-level contradiction checks.

Markdown remains the knowledge source of truth. SQLite coordinates short local
operations and derived indexes only. Operational databases use rollback-journal
mode because the bundled SQLite 3.50.4 is affected by the documented WAL-reset
bug. No daemon, cloud service, dashboard, or automatic Git operation is added.

Uncertain semantic claims are quarantined automatically. They cannot supersede
active knowledge until a frozen benchmark establishes the required risk bound.
Routine low-risk recovery remains automatic and agent-readable.

## Evidence

- User-approved Stage 2 scope supplied in the 2026-07-13 architecture review;
  implementation details are research-derived and recorded in the linked design.
- Design: `docs/superpowers/specs/2026-07-13-reliable-memory-design.md`.
- SQLite atomic commit: https://www.sqlite.org/atomiccommit.html
- SQLite WAL-reset notice: https://www.sqlite.org/wal.html
- RFC 8493 BagIt: https://www.rfc-editor.org/rfc/rfc8493.html
- W3C PROV-DM: https://www.w3.org/TR/prov-dm/

## Related

- [[knowledge/notes/agent-native-mcp-foundation]]
- [[knowledge/notes/centralized-memory-subsystem]]
- [[solo-operator-superset-product-decision]]
- [[knowledge/notes/durable-capture-producer-activation-decision]]
- [[knowledge/notes/integration-config-backup-retention-decision]]
- [[knowledge/notes/reliability-v3-runtime-adoption-implementation-decision]]
- [[knowledge/notes/observable-capture-and-bounded-maintenance-decision]]
- [[knowledge/notes/oversized-daily-compile-decision]]
- [[knowledge/notes/system-symlink-ancestor-decision]]
- [[knowledge/notes/citation-relevance-gate-decision]]
- [[knowledge/notes/dead-task-retirement-and-restore-decision]]
- [[knowledge/notes/daily-entry-boundary-decision]]
