---
type: decision
status: active
confidence: high
source_authority: user
date: 2026-08-22
---

# Retry After Quarantine Takes A New Attempt Id

One-sentence summary: a refused write keeps its idempotency key and its evidence,
and the next attempt over the same inputs becomes a new operation with the next
ordinal instead of being refused forever.

## Context

A compile reached its write and was refused there by the fail-closed DLP
boundary, so its transaction ended `quarantined`. A compile's operation id is
derived from its inputs, so the next attempt over the same daily logs produced
the same id. Its request hash differed — the refusal itself had been fixed and
the bytes to be written had changed — and the coordinator answered
`operation_id is already bound to a different request`.

That refusal is correct. What was wrong is that nothing could follow it. `prune`
takes only `committed` and `discarded` after thirty days, `recover` takes only
`aborting`, `aborted`, `preparing`, `prepared` and `applying`, and no operator
command disposes of quarantine. One refused attempt locked those dailies out of
compiling permanently.

## Decision

A key is never reused for a different payload; that rule stays exactly as it was.
A quarantined attempt wrote nothing durable, so the attempt that follows it is a
**new operation**: it takes the next ordinal (`<operation-id>#2`, `#3`, …) and
names the refused transaction as its parent. Quarantined rows are never modified
and never deleted. An unchanged payload still resolves to the base id, so crash
resume and ordinary idempotency are untouched. A hundred ordinals is the bound;
past that the caller is told, because a hundred refusals is an operator's problem
and not something to keep numbering.

`MarkdownCoordinator.attempt_operation_id` answers "which id must this attempt
use, and what does it follow", and the compile asks it immediately before
preparing.

## Why this shape

Current practice is explicit that the refusal must stand: the IETF
`Idempotency-Key` draft says a key "MUST not be reused across different payloads
of this operation", and Stripe's idempotency layer compares incoming parameters
against the original request and errors when they differ. Stripe releases a key
only when execution never began; ours had begun and been refused at publication,
so releasing it would contradict both the standard and this vault's contract that
quarantine is retained evidence. A changed payload is therefore a new operation
and needs a new key.

The shape is not invented here. Project checkpoints in the same module have
always retried this way: on `quarantined` they increment `attempt_number`, derive
a new operation id containing that ordinal, record `parent_operation_id`, and
insert an immutable attempt row. The migration validator already enforces the
result — attempts numbered `1..n`, each linked to its predecessor, every attempt
but the last quarantined. The transaction table carried the same vocabulary in
`parent_transaction_id` and `prepare` accepted it; no caller passed it, so the
schema had the chain and the behaviour was missing.

## Evidence

- Two quarantined compile transactions in this vault, `87142ff7b1cc42c0` at
  2026-08-22T07:28:49Z and `a2bd6b02a0e0461b` at 2026-08-22T10:45:41Z, both
  `dlp_content_blocked`.
- The nightly service then failed with
  `ValueError: operation_id is already bound to a different request`.
- Two regressions in `tests/test_compile_transactions.py` drive a compile into
  quarantine and retry it; both fail without this change with that exact error.
- Research: `docs/research/2026-08-22-idempotency-retry-after-quarantine.md`.

## Open questions

Whether an operator also wants a deliberate command to dispose of quarantined
evidence. With the retry chain in place nothing is blocked by keeping it, so the
question is retention, not liveness.

## Related

- [[knowledge/notes/reliable-memory-stage-2]] — the transaction boundary this
  extends.
- [[knowledge/notes/v4-reliability-contracts-decision]] — the reliability
  contracts this stays inside.
- [[knowledge/notes/self-resolving-health-findings-decision]] — how a refused attempt stops being an open finding once its work happened.
- [[knowledge/notes/bounded-read-is-not-corruption-decision]] — why the quarantine record this retry leaves behind is still an unresolved finding, and what was refused rather than relax it.
