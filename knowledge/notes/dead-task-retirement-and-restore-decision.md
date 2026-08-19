---
type: decision
title: "Dead Task Retirement And Restore"
description: "A task whose attempts are exhausted is retired only when asked for by name, through the same verified export, and one command brings its work back."
date: 2026-08-19
confidence: high
source_authority: user
status: active
---
# Dead Task Retirement And Restore

One-sentence summary: A task whose attempts are exhausted is retired only when asked for by name, through the same verified export, and one command brings its work back.

## Decision

Date: 2026-08-19.

`memory_queue.py purge` gains `--include-dead`. Without it the selection is
unchanged — `succeeded` and `cancelled` older than the cutoff. With it, `dead`
tasks join the same path: export first, one manifest with a digest per result,
re-read and verify every byte, then delete inside one transaction that re-checks
the selection.

`memory_queue.py restore --export <path>` reads one export back. It verifies the
manifest, the records digest, every result digest, and that the record ids match
the manifest, then re-enqueues each record's kind, handler version, payload, and
priority as a new ready task. The receipt maps each exported id to the new one.
A single failed check restores nothing.

## Why

The original criterion asked for a retention path with a verifiable manifest,
deletion, and restoration. Removing the unsafe `clear-failed` command closed the
dangerous half and left the other half missing: a dead task could never be
retired, and nothing could be brought back once exported.

Dead tasks are not noise. A task reaches that state after its attempts are
exhausted, so it records work the system promised and never did. That is why the
default keeps them and why retiring them is a named, explicit act rather than a
side effect of routine cleanup.

Restoration re-enqueues rather than reinserting the original rows. A purged
task's identity and attempt history are gone by design — reviving them would
resurrect immutable history that the export already holds. What the operator
actually wants back is the work, so the work is what returns, with a receipt
that ties it to the exported record.

## Consequences

- `run/` deletion contract is unchanged: retained work and results still block it.
- A restored task is new: its id differs, its attempts start at zero, and the
  export remains the record of what happened before.
- An operator who purges dead tasks without keeping the export loses the ability
  to restore them. The export path is required, as before.
- Failed-but-retryable tasks are not purgeable at all; they are still eligible
  work, not terminal evidence.

## Source / Evidence

- Explicit operator approval, 2026-08-19; audit item OPEN-020.
- Selection, export, and verified deletion: `scripts/memory_queue.py::MemoryQueue.purge`.
- Verified restore: `scripts/memory_queue.py::MemoryQueue.restore`.
- Regressions: `tests/test_memory_queue_migration.py` — retention by default,
  purge with `--include-dead`, restore from a verified export, and a tampered
  export that restores nothing.

## Related

- [[reliable-memory-stage-2]]
- [[v4-reliability-contracts-decision]]
