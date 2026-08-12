---
type: decision
title: "Cold Archive For Disconnected Retired Compile Evidence"
description: "Move only whole retired manifest/journal components outside the live recovery closure into sealed, byte-exact transactions under run/compile-archive/."
timestamp: 2026-08-12T01:53:17
confidence: high
source_authority: user
---
# Cold Archive For Disconnected Retired Compile Evidence

One-sentence summary: Move only whole retired manifest/journal components outside the live recovery closure into sealed, byte-exact transactions under `run/compile-archive/`.

## Decision
Date: 2026-08-12.

Chose: add a compiler-owned cold archive with the operator flow `audit` ->
`backup-only` -> reviewed `apply` -> `verify`. The audit treats compile state,
queue records, active manifests, and active journals as live roots. It follows
manifest generation/batch links and journal generation/batch links, and may
archive only whole disconnected retired components. `backup-only` copies exact
bytes into a sealed transaction. Apply accepts only that manifest with its
top-level `approved` field changed to `true`, revalidates the live closure and
file identity under the compile lock, then removes the hot retired names through
a resumable staging journal.

Cold entries are preservation records, not an automatic replay tier. Automatic
replay continues to use only active and hot retired stores; restoring cold
evidence requires a separate reviewed recovery operation.

Rejected: deleting old retired records, increasing the bounded hot-store quota,
or archiving individual records without their connected manifest/journal
component. Deletion loses evidence, a larger quota only postpones exhaustion,
and partial components can make replay provenance ambiguous.

Why: the hot retired store is intentionally bounded, but completed evidence may
need longer preservation than the replay working set. A cold transactional tier
releases bounded hot capacity without treating age as proof that evidence is
dead and without changing `$LLM_WIKI_ROOT` or `$LLM_WIKI_STATE_ROOT`.

## Safety Contract
- Preserve source bytes exactly and verify SHA-256 and size before hot removal.
- Reject symlinks, reparse points, hard links, malformed JSON, ambiguous IDs,
  directory replacement, and source identity drift.
- Retain the original hot inode under the sealed transaction's
  `source-retired/` tree instead of unlinking by a raced pathname; keep the
  separate `payload/` copy as the reviewed backup image.
- Persist staging progress before destructive steps so interrupted apply is
  idempotently resumable from the same approved manifest.
- Never archive a component reachable from state, queue, or active compile
  artifacts.

## Evidence
- User-approved recovery direction recorded in the private 2026-08-12 session.
- Python 3.10 `os.fsync` and file APIs:
  https://docs.python.org/3.10/library/os.html
- Windows file identity and durable move APIs:
  https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getfileinformationbyhandle
  and https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-movefileexa

## Related
- [[knowledge/notes/centralized-memory-subsystem]]
- [[docs/operating-model]]
