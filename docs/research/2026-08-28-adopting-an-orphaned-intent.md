# Adopting an orphaned intent

Date: 2026-08-28.
Question: a capture intent is written durably to `run/capture-intents/ready/`
and recorded in `capture_intents`, and then the enqueue that would give it a
worker fails. The record is committed; nothing will ever dispatch it. Before
building a recovery path: how do durable-work systems recover a record that was
committed but never dispatched, and what makes adopting one safe here?

A correction belongs at the top of this note, because it changes what the work
is for. `NEW-136` reported **10 orphaned intents** on the live vault. Measured
today, the live count is **zero** — see "What the live vault actually holds"
below. The hole is real and reachable; it is not currently occupied. This note
argues a recovery path for a latent hole, which is a weaker claim than the one
the audit entry made and is the honest one.

## Finding 1 — this is the outbox relay problem, and the answer is a sweeper, not a stronger write

The shape is exactly the transactional outbox. An application commits business
state and an event row in one transaction, and a separate **relay** reads
unpublished rows and publishes them. The literature is unanimous that the relay
is where recovery lives: microservices.io's statement of the pattern has the
message relay reading the outbox and publishing, and explicitly accepts that it
"might publish a message more than once (e.g. crash after publishing but before
recording it)". AWS Prescriptive Guidance describes the same split — the events
processing service reads the outbox table and "recognizes only those rows that
are part of a committed transaction".

The load-bearing point for us: in every description, the relay is a **standing
sweeper over committed-but-unsent rows**, not a retry bolted onto the writer.
The writer's job ends when the row is durable. A 2025 practitioner treatment
(softwarecraftsperson.com) names the failure mode we have by name — the relay
"can fail silently ... yet the application continues to commit events to the
Outbox, and without dedicated lag monitoring, the system can appear healthy
while thousands of events pile up". That is precisely what a ready intent with
no `capture_task_links` row is: a committed outbox row with no relay.

LLM Wiki's capture path has the outbox halves inverted from the classic layout —
the durable record is a file plus a `capture_intents` row, and the "publish"
step is `enqueue_capture_task_replay_safe` — but the gap is identical, and so is
the remedy. There is no sweeper. `recover_expired_leases` is the only recovery
the queue has, and it is about a task whose **lease** expired, which presupposes
a task. An intent with no task is invisible to it.

## Finding 2 — at-least-once plus an idempotency key is the accepted trade, and we already have both halves

The 2026 references agree that when a system must choose, it takes duplication
over loss: "every serious provider picks at-least-once because a dropped payment
is worse than a duplicated one" (Hooklistener, 2026), and the recommended
implementation is to "attach an idempotency key, check a deduplication store
before acting, and let at-least-once redelivery be harmless". The same source
names the detail teams skip: the key is only useful if a durable store records
that the key has been processed *before or atomically with* the side effect —
commonly "a unique-constrained database column".

This vault is on the good side of that warning already, and by construction
rather than by convention:

- `enqueue_capture_task_replay_safe` computes `capture:{intent_id}:{handler_version}`
  as `dedupe_key` and, before enqueueing anything, looks for an existing
  `capture_task_links` row for that `intent_id`; if one exists it returns the
  existing binding instead of creating a second task.
- `capture_task_links.intent_id` is the deduplication store, in the same SQLite
  database as `tasks`, written in the same `begin_immediate` transaction as the
  task row (`_insert_capture_task_row`). The side effect and the dedupe record
  are one commit — exactly the atomicity the reference asks for.

So adoption does not need to invent idempotency. It needs to call the enqueue
that already has it. Adopting an intent twice must yield one task because the
second call finds the link row from the first.

## Finding 3 — what makes adoption safe here is that the intent is create-only and self-describing

The reason a sweeper is safe in the outbox pattern is that the row it re-reads
is immutable: the relay cannot corrupt state by re-publishing, only duplicate.
The same three properties hold for a capture intent, and they are what makes
adoption a *read* of durable evidence rather than a re-derivation:

1. **Create-only.** The intent file is published with `create_only=True` and is
   never rewritten. `capture_intents` rows move `pending → ready` once and are
   not otherwise mutated.
2. **Self-verifying.** The row carries `intent_sha256` and `byte_size`. Adoption
   can hash the file it is about to enqueue and refuse if the bytes moved, so a
   damaged record becomes a named skip rather than a task that will fail at the
   worker.
3. **Self-addressing.** The row carries `relative_path`. The payload the adapter
   enqueues is exactly `{intent_id, intent_path, intent_sha256}`, all three of
   which are columns. Adoption reconstructs a byte-identical payload from the
   record without needing the session, the transcript, or the original process.

Because of (3), adoption does not have to reconstruct the lost fence's context.
It reconstructs the *payload*, and takes its own fresh authority.

## Finding 4 — the fence must be re-taken, never reused, and that is not a weakening

The tempting shortcut is to let adoption pass the old fence, or to relax
`_require_live_capture_fence`. Both would be wrong, and the outbox literature
says why in its own terms: the relay is a **separate actor** with its own
identity, not a resurrection of the writer. The writer's fence expired because
the writer is gone; that is the fence working, not failing.

`_require_live_capture_fence` checks eight things about the fence — token,
epoch, the owner's role/scope/actor/token/epoch, the owning process's pid and
start identity — plus `expires_at > now`. Every one of those is a statement
about *the process currently holding the intent*. An adopting process satisfies
all of them honestly by acquiring its own capture owner and its own intent fence
under its own pid, which is what `_capture_publication_fence` already does for
the publisher. Adoption therefore uses the identical acquisition, and the fence
check passes because it is true, not because it was loosened. No line of
`_require_live_capture_fence` needs to change.

One consequence has to be stated rather than designed around:
`acquire_intent_fence` refuses with `intent_fenced` if **any** row exists for
that `intent_id`, with no expiry test. If a publisher died holding a capture
fence, that row blocks adoption too. Adoption must treat that as a named skip
and report it, not force it — deleting another actor's fence row is exactly the
weakening this finding rejects. Measured today the live `intent_fences` table
holds zero rows, so this is a latent case, and it is recorded here as the known
limit of the path rather than fixed inside it.

## Finding 5 — a bound is required because the sweeper shares a pass with the worker

Both references that discuss relay operation treat batch size as a required
parameter, not an optimisation: the relay reads a bounded page of unsent rows
per poll so a backlog cannot monopolise the process. Here the sweeper runs at the
head of `run_capture_worker_once`, which is spawned detached on every session
end and is expected to finish; and again as a nightly step. An unbounded pass
over a large backlog would delay the capture that just woke it.

The live vault publishes on the order of ten intents a day (24 intents span
2026-08-26 to 2026-08-28). A cap of 32 covers roughly three days of backlog in
one pass, so a weekend of failures is cleared by the first Monday capture,
while the worst case stays a few seconds of enqueue work. Anything left over is
adopted by the next pass — the sweeper runs on every capture and every night, so
a bound defers work, it never drops it.

## What the live vault actually holds

Measured 2026-08-28 against `run/queue-v3.sqlite3`:

- `capture_intents`: 24 rows, all `publication_state='ready'`.
- `capture_task_links`: 24 rows. **Intents with no link at all: 0.**
- The linked tasks: 14 in state `ready` (waiting for a worker), 10 `succeeded`.

`24 - 14 = 10` reproduces the audit's "10 orphaned" exactly, and the query that
produces 14 is "intents whose task is in state `ready`". The ten intents the
audit called orphaned are the ten whose capture task **already ran to
completion**. They are the pipeline working, not the pipeline losing.

The audit's causal story does not survive contact with the trail either. Both
live failure rows are labelled `adapter_unknown`, and that label is produced by
`_record_cli_capture_failure` as `f"adapter_{args.event or 'unknown'}"`. Only two
adapter invocations carry no `--event`: `--capture-worker` and `--maintenance`.
So the 22 `intent_fence_lost` occurrences come from a process with no lifecycle
event — the capture **worker**, whose `_require_live_worker_intent`
(`memory_queue.py:8149`) raises the same string for `mode='worker'` — not from
the publisher's `_require_live_capture_fence` (`:7634`) as the audit entry
assumed. Two different fences, one error string, and a trail label that named
neither.

That the hole is nevertheless real was established by reproduction, not by
reading: publishing an intent to `ready` and then releasing the fence before
enqueue leaves `capture_intents`: 1, `capture_task_links`: 0, the full record on
disk, and `intent_fence_lost` raised — and no code path anywhere enumerates
ready-but-unlinked intents.

## Conclusion

Build the relay the pattern calls for: a bounded sweeper over ready intents with
no `capture_task_links` row, taking a fresh capture owner and a fresh intent
fence per intent, verifying the record's own `intent_sha256` before enqueueing,
and reaching the queue through `enqueue_capture_task_replay_safe` so the
existing `capture:{intent_id}:{handler_version}` dedupe makes a second adoption
a no-op. Run it at the head of the capture worker, where `recover_expired_leases`
already sits for the same reason, and again as a nightly step so recovery does
not depend on another capture ever arriving.

Fix the trail label in the same pass. The audit's mechanism was wrong for one
reason only: the row said `adapter_unknown` when the process knew perfectly well
what it was.

Sources:
- [Pattern: Transactional outbox — microservices.io](https://microservices.io/patterns/data/transactional-outbox.html)
- [Transactional outbox pattern — AWS Prescriptive Guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
- [Transactional Outbox Pattern: A Practical Guide to Trade-offs](https://www.softwarecraftsperson.com/posts/2025-10-08-transactional-outbox-pattern/)
- [Webhook Idempotency and Deduplication (2026) — Hooklistener](https://www.hooklistener.com/learn/webhook-idempotency-and-deduplication)
- [Idempotency Keys Explained: Safe API Retries in 2026](https://www.alekseialeinikov.com/en/blog/topics/architecture/idempotency-in-practice-api-retries-2026)
