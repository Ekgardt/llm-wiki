---
type: decision
title: "Durable Capture Producer Activation"
description: "SessionEnd and PreCompact capture publish durable Reliability V3 intent evidence before returning; detached work only wakes recovery and deletion requires terminal proof."
date: 2026-08-16
confidence: high
source_authority: user
status: active
---
# Durable Capture Producer Activation

One-sentence summary: SessionEnd and PreCompact capture publish durable Reliability V3 intent evidence before returning; detached work only wakes recovery and deletion requires terminal proof.

## Decision

Date: 2026-08-16.

The user approved activating the durable capture producer, so lifecycle capture stops
depending on a detached child surviving long enough to record what happened. It
implements the capture-intent part of
[[knowledge/notes/v4-reliability-contracts-decision]] without adding a database,
runtime root, daemon, or MCP tool.

- The canonical `integration_adapter.py` publishes a create-only `capture-intent/v1`
  record for bounded SessionEnd and PreCompact transcript evidence **before** the hook
  returns to its host.
- Publication is file-first: pending and ready states are reconciled against the
  active validated Queue/Coordinator v3 pair.
- Detached work only wakes recovery. It is never the thing that makes evidence
  durable, so a killed child cannot lose a session.
- Exact replay reuses the existing binding or terminal record. Conflicting bytes for
  an already-published intent are rejected rather than overwritten.
- The worker resumably publishes immutable decision and terminal evidence without
  repeating a provider call or a Markdown side effect.
- Source cleanup requires immutable terminal proof: committed Markdown, a validated
  no-durable-content result, or an explicit operator discard. Queue enqueue alone
  never authorizes deletion.
- Ordinary purge is export-first and crash-resumable, and retains task authorization.

## Rejected Alternatives

- Treating a successful queue enqueue as durability was rejected because the queue is
  coordination state, not evidence that the session content survived.
- Letting the detached child own publication was rejected because host shutdown can
  kill it before any durable record exists.
- Overwriting a conflicting intent for an existing occurrence was rejected because it
  would silently replace captured session evidence.

## Acceptance

Completion requires regressions for crash boundaries, exact replay, conflicting-byte
rejection, resumable terminal publication, and deletion refusal without terminal
proof. Live repeated `/compact` and path-based PreCompact evidence remains external
and is tracked as `EVID-009`.

## Source / Evidence

- `knowledge/log.md`, entry dated 2026-08-16.
- `docs/DEVELOPER-AUDIT-STATUS-2026-08-14.md`, item `OPEN-012` (`CLOSED_CODE`), which
  records the implemented behaviour and its regression suites.
- `CLAUDE.md` / `AGENTS.md`, "Approved Reliability v3 target" section.
- `docs/STRUCTURE.md`, operational migrations section.
- `scripts/integration_adapter.py`
- `scripts/memory_queue.py`

## Editorial note

This page was reconstructed on 2026-08-17 from the in-repository records listed above.
`knowledge/index.md` and `docs/STRUCTURE.md` already cited it, but the page itself was
never published to the public source, so both references were dangling. The content
restates only what those records state; no new decision is introduced here.

## Related

- [[knowledge/notes/v4-reliability-contracts-decision]]
- [[knowledge/notes/reliability-v3-runtime-adoption-implementation-decision]]
- [[knowledge/notes/agent-native-mcp-foundation]]
- [[knowledge/notes/reliable-memory-stage-2]]
- [[knowledge/notes/observable-capture-and-bounded-maintenance-decision]]
