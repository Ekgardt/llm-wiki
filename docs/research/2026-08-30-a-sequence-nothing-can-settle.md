# A sequence nothing can settle

Dated 2026-08-30. Written before changing how project-checkpoint recovery
works, because the change decides what happens to a write that failed.

## The state on this vault

Three checkpoint rows are not committed, and the project they belong to has
stopped writing entirely.

```
llm-wiki 1428  quarantined   68 evidence ids   transaction 4ab2ceba… quarantined
llm-wiki 1429  reserved      28 evidence ids   no transaction at all
fix-pip   320  reserved       ?                transaction 20477c3f… discarded
```

`llm-wiki` 1429 carries exactly the 28 events at the head of the pending queue,
and they are a strict suffix of 1428's 68. The other 40 of 1428's ids are
**neither in the queue nor in the journal**. Grepped: zero occurrences across
`journal.md` (sequences 1001–1427) and the sealed `journal.000001-001000.md`.
The only copies on disk are inside the quarantined transaction's own after
image, `run/transactions/4ab2cebadbfc.../after/000000.bin`, and an orphaned
`state.json` snapshot.

So 1428 is not a stale duplicate. It is the only surviving record of 40 events,
and it is the row that blocks everything behind it.

## Why nothing moves

`_check_project_head` refuses any sequence while an earlier one is not
committed. That is the ordering guarantee and it is right.

What is missing is the other half. `ProjectStore.recover` settles only rows in
`prepared` and `reserved`; a `quarantined` checkpoint row is never touched by
anything. `_retry_spent_checkpoint` — the mechanism
[[idempotent-retry-after-quarantine-decision]] established, which re-attempts a
quarantined sequence under a new attempt number while preserving the refused
attempt in `project_checkpoint_attempts` — is reachable only when a new request
arrives bearing the same `occurrence_id`. After the batch-identity fix earlier
today, no new request ever will: names are now derived from batch membership,
and 1428 was named under the old scheme.

A quarantined sequence is therefore a permanent hole, and every later sequence
queues behind it. That is head-of-line blocking, and the literature's answer is
either to fix the entry or to route it aside — never to leave the queue
blocked on it indefinitely.

There is a second, quieter fault. `_reserve_new_checkpoint` allocates
`MAX(sequence) + 1` no matter what is unsettled below it. That is how 1429 came
to exist behind a stuck 1428: a doomed reservation, created because nothing
refused it at reservation time. Refusing later, at apply time, forever, is the
worst place to refuse.

## What the field says

For an ordered log, the standing advice on a poison entry is to fix it or move
it aside so the rest can proceed; leaving the head blocked is the failure, not
the entry.

For the duplicate that recovery creates, the standing advice is a dedup layer:
track what has been processed and skip an entry already applied, rather than
appending another copy. The specific form recommended is exactly the one
available here — a set of already-processed ids consulted before the append.

## What the code already intends

`tests/test_project_journal.py` states the contract plainly, and it is a good
one: a quarantined prior blocks a later sequence with `ProjectPendingPriorError`,
and the block lifts when **the original request comes back by name** —
`store.checkpoint("demo", first_event, ...)` re-derives the same
`occurrence_id`, `_retry_spent_checkpoint` fires, and both sequences append in
order. Seven tests encode it.

So the design is not missing a settlement path. What broke it here is narrower:
earlier today the batch name became a function of batch membership, and the
three unsettled rows were named under the old scheme. No future request can
ever bear their names, so the one door the design left open is, for them alone,
walled up.

## Two designs tried and rejected

**Recovery settles quarantine on its own.** Implemented, then reverted: it
fires during `checkpoint()`, which calls `recover()` first, so it lifts the
block before the second agent's request is refused. Seven tests failed, and
they were right to — a quarantined head is the requester's to clear, not
recovery's.

**A reservation whose evidence is already journaled is settled without a second
write.** Implemented, then reverted. It rests on evidence ids being unique per
event, which holds for drain batches but is not a property of the schema:
`tests/test_project_journal.py` builds every event with the same
`evidence_event_ids`, and the rule silently swallowed the second checkpoint.
Twelve tests caught it. A rule that can drop a real write is not worth the
duplicate it saves.

## The decision

No permanent change to recovery or to the ordering rule. The prevention landed
earlier today — batch names derived from membership, spent reservations retried
instead of reported done, bounded drain windows, and a `checkpoints` health
finding — and it is enough that this cannot recur.

What remains is a **one-time repair of the rows the rename orphaned**, run
explicitly: `scripts/repair_orphaned_checkpoint_names.py`. Its criterion is
exact and self-limiting — a non-committed checkpoint whose `occurrence_id` was
not produced by the batch naming, which after today no request can ever
produce again. It takes the project lease, gives each such row a fresh attempt
through the same `_retry_spent_checkpoint` the design already uses, replays it,
and reports what it did. On a vault with no orphaned names it does nothing.

The one cost, stated rather than hidden: `llm-wiki` 1429 carries 28 events that
are a suffix of 1428's 68. Once 1428 is written, appending 1429 cites those 28
a second time. The projection folds deltas by id, so the rendered state is
unaffected; the journal keeps two citations of the same events. That is the
price of not losing the 40 events that exist nowhere else, and it is the right
side of that trade.

## A gap I made, and the repair for it

The second rejected design did damage before it was reverted. While
`_already_journaled` was in the tree, hooks kept running against this vault,
and `llm-wiki` 1429 was marked **committed with nothing written**: the
containment test passed, the row was settled, the drain deleted its 28 events
from the queue, and no journal entry was appended.

Nothing is lost — those 28 events are a strict suffix of 1428's 68 and 1428 is
journaled — but the invariant is broken: the coordinator calls 1429 committed
while the journal ends at 1428, and 1430 then refuses to follow with
`ProjectJournalRebuildRequired`, which is the exception whose own docstring
says an explicit verified rebuild is required. No such rebuild existed.

`scripts/repair_journal_gap.py` is that rebuild, and it is deliberately narrow.
It walks forward from the journal head one sequence at a time. For each, it
appends only when the coordinator holds a committed row for exactly
`head + 1`, the journal carries no record with that sequence, and the project
lease is held. It re-attempts the row through the same step every other retry
uses, so the append goes through the ordinary transaction with its own
before/after images. It stops at the first sequence that does not meet all
three conditions, and on a vault with no gap it does nothing.

The one cost is the one this note already predicted for this pair: 1429's entry
cites 28 events that 1428's entry also cites. The projection folds deltas by id,
so the rendered state is unchanged; the journal keeps two citations. Agreement
between the database and the journal is worth that.

## Sources

- [Queue Despair: Ordering and Poison Messages](https://www.openmymind.net/Queue-Despair-Ordering-And-Poison-Messages/)
- [Head-of-line blocking](https://en.wikipedia.org/wiki/Head-of-line_blocking)
- [How to Handle Poison Messages in Kafka — OneUptime](https://oneuptime.com/blog/post/2026-01-21-kafka-poison-messages/view)
- [Idempotent Consumer Pattern — Pradeep Loganathan](https://pradeepl.com/blog/patterns/idempotent-consumer-pattern/)
- [Build Idempotent Kafka Consumers: Patterns That Actually Work — Conduktor](https://www.conduktor.io/blog/building-idempotent-consumers)
- [Idempotency & Deduplication — System Design Sandbox](https://www.systemdesignsandbox.com/learn/idempotency-deduplication)
- [Idempotency in Streaming Pipelines — Streamkap](https://streamkap.com/resources-and-guides/idempotency-streaming-pipelines)
