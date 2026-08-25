# Retrying after a refused attempt without reusing its idempotency key (2026-08-22)

## Why this was researched

A compile that reached the write and was refused there left its transaction
`quarantined`. The operation id of a compile is derived from its inputs, so the
next attempt over the same dailies produced the same id with a different request
hash — because the refusal itself had been fixed and the bytes to be written had
changed — and the coordinator answered
`operation_id is already bound to a different request`.

The effect outlives the incident: one refused attempt locks those inputs out of
compiling for good. `prune` takes only `committed` and `discarded` after thirty
days, `recover` takes only `aborting`, `aborted`, `preparing`, `prepared` and
`applying`, and nothing disposes of quarantine. This is a change to the
reliability core, so rule 2 applies.

## What current practice says

The refusal itself is correct and must stay. The IETF `Idempotency-Key` header
draft is explicit: a key "MUST not be reused across different payloads of this
operation", and a server that sees one SHOULD answer 422. Stripe implements the
same rule — its idempotency layer "compares incoming parameters to those of the
original request and errors if they're not the same to prevent accidental
misuse" — and it stores the outcome of the first request under a key whether
that outcome was success or failure.

Stripe draws one boundary that matters here: a result is saved only once
execution of the endpoint has begun. A request rejected before execution leaves
no saved result and may be retried under the same key. Our quarantined attempt is
on the far side of that line — it prepared, staged its after-images and was
refused at publication — so releasing its key would contradict both the standard
and this project's own contract that quarantine is retained evidence.

The conclusion current practice points to is therefore not "release the key" but
"a changed payload is a new operation and needs a new key".

## What this project already does

The same file already implements exactly that for project checkpoints. When a
checkpoint sits in `quarantined`, the next reservation increments
`attempt_number`, derives a **new** `operation_id` that includes the attempt
ordinal, records `parent_operation_id`, and inserts an immutable row into
`project_checkpoint_attempts`. The migration validator enforces the shape:
attempts are numbered `1..n`, each links to its predecessor, and
"every attempt but the last must have been quarantined".

The transaction table carries the same vocabulary — `parent_transaction_id` — and
`prepare` accepts it, but no caller passes it, so the transaction path has the
schema for the chain and none of the behaviour.

## What this changes here

The compile asks the coordinator for the id its attempt must use. If nothing is
bound, or what is bound is not quarantined, that is the base id and everything
behaves exactly as before, including crash resume under an unchanged payload. If
the bound record is quarantined, the attempt takes the next ordinal and names the
quarantined transaction as its parent. Quarantined rows are never modified, and
the coordinator keeps refusing a reused key with a different payload — the
refusal simply stops being a dead end.

## Sources

- IETF httpapi WG, "The Idempotency-Key HTTP Header Field" (draft-07) —
  https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-idempotency-key-header-07
- Stripe API reference, "Idempotent requests" —
  https://docs.stripe.com/api/idempotent_requests
- Stripe engineering, "Designing robust and predictable APIs with idempotency" —
  https://stripe.com/blog/idempotency

## Open question

Whether an operator also needs a deliberate command to dispose of quarantined
evidence is not answered here. With the retry chain in place nothing is blocked
by keeping it, so the question is about retention, not about liveness.
