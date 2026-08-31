# A batch named after one of its members

Dated 2026-08-30. Written before changing how a project checkpoint is
identified, because identity under retry is an idempotency contract.

## What is happening

With the drain-window fix in place, the state-lock timeouts stop and the drain
reaches the coordinator. It then fails, on every attempt:

```
project checkpoint: ValueError: occurrence_id is already bound to another event
```

Eight in five minutes, and the queue of 2 464 pending checkpoints does not
move.

Instrumenting the comparison the coordinator makes shows exactly one field
differs between the stored reservation and the requested one:

```
DIFF evidence_event_ids:
  stored    = ["b9bdf169...", "cc3d8ebd...", "64938c50...", ...]
  requested = ["bf6e0938...", "25e9f587...", "7a1fbedd...", ...]
```

Same `occurrence_id`. Entirely different membership. The coordinator is right
to refuse; the caller is wrong to have asked.

## Why

`_merge_pending_checkpoints` names the batch after its last member:

```python
event_id = str(items[-1]["event_id"])
checkpoint.update({
    "occurrence_id": event_id,
    "idempotency_key": f"{event_id}:{decision.reason}",
    ...
})
```

The last member is not the batch. Two batches that end at the same event but
carry different earlier events are two different operations wearing one name.
That is what a reservation refuses, and the refusal is permanent: the stored
row never expires, so every later attempt to commit any batch ending at that
event fails the same way, forever.

This became reachable because the batch boundary moved. During the journal
outage of 08-28 a batch was reserved and never committed; the queue kept
growing; a later cycle formed a different batch ending at the same event. The
name collided. Nothing can drain past it.

## What the field says

The distinction the sources draw is exactly the one that was missed here:
"content hashing detects duplicates; an idempotency key identifies one intended
operation — different jobs." The warning against hashing a request body is that
"two legitimately identical requests would collapse into one, silently losing a
real order."

That warning does not apply to this queue, and it is worth saying why rather
than waving it away. Two batches carrying the same ordered
`evidence_event_ids` are not two legitimately identical requests. Each element
is a unique event identifier that appears in the queue once and is deleted from
it on commit. Identical membership therefore means one operation retried, which
is the case the reservation exists to collapse.

For that case the recommended derivation is the one being adopted:
cryptographic hashing with SHA-256, which "ties the idempotency key to the
exact request intent, verifying that payload parameters match perfectly during
retries." Concatenation is explicitly discouraged in favour of a cryptographic
hash.

## The decision

Derive the batch's identity from the whole batch:

```python
occurrence_id = "batch:" + sha256(canonical(evidence_event_ids))
idempotency_key = f"{occurrence_id}:{reason}"
```

- A retry of the same batch produces the same name, so the reservation
  collapses it as a duplicate — the behaviour the current scheme intended.
- A batch with different membership produces a different name, so it can never
  claim a reservation that belongs to another batch. The failure mode above
  becomes unreachable rather than merely rarer.
- The identity is unchanged for a single-event batch in every respect that
  matters, but its *string* changes, so the wedged reservations from 08-28 no
  longer stand in the way.

That last point must be stated plainly: this does not repair the stale
reservation rows. It stops colliding with them. They remain in the coordinator
database as reserved-but-never-committed evidence of the outage, which is what
that database is for.

The schema permits it: `occurrence_id` and `idempotency_key` are strings of 1
to 256 characters with no format constraint.

## What this does not settle

Whether a reservation should expire. A reservation that is never committed and
never abandoned blocks its name forever, and here that was indistinguishable
from a permanent wedge until the event bodies were diffed by hand. A bounded
reservation lifetime, or a doctor finding for reserved-but-uncommitted rows
older than some age, would have surfaced this in a minute rather than a day.
Both are larger changes than this one and belong to the owner.

## Sources

- [How to Create Event Idempotency Keys — OneUptime](https://oneuptime.com/blog/post/2026-01-30-event-idempotency-keys/view)
- [Idempotency Keys Explained: Safe API Retries in 2026](https://www.alekseialeinikov.com/en/blog/topics/architecture/idempotency-in-practice-api-retries-2026)
- [Idempotency in Distributed Systems: Design Patterns Beyond "Retry Safely"](https://aloknecessary.in/blogs/idempotency-distributed-systems/)
- [Implementing Idempotency Keys in REST APIs — Zuplo](https://zuplo.com/learning-center/implementing-idempotency-keys-in-rest-apis-a-complete-guide)
- [Idempotency — System Design (AlgoMaster)](https://algomaster.io/learn/system-design/idempotency)
