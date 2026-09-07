---
type: decision
status: active
confidence: high
source_authority: ai-derived
date: 2026-08-30
---

# An Identity Names What It Identifies, Or It Is A Wedge

One-sentence summary: a batch is named after the whole batch and a reservation
dies with its transaction, because an identity derived from a part of a thing
will eventually be claimed by two different things and refuse the second one
forever.

## Context

On 2026-08-30 the project-checkpoint path had been stopped for two days and
nothing said so. `run/state.json` held 4.8 MB of pending checkpoints — 2 537 for
`llm-wiki`, 744 for a second project — and `logs/hook-errors.log` had 1 603 lock
timeouts and 366 refusals reading `operation_id is already bound to a different
request`.

Three separate causes, one shape. Each was an identity that outlived the thing
it identified.

**A batch named after its last event.** `occurrence_id` was
`items[-1]["event_id"]`. Two batches ending at the same event but carrying
different earlier events are two operations wearing one name. Diffing the two
bodies the coordinator compares showed exactly one differing field,
`evidence_event_ids`. The coordinator refused the second batch, correctly, and
the refusal is permanent because the reservation never expires.

**A reservation that outlived its transaction.** A `reserved` row whose
transaction ended `discarded` was answered `duplicate=True` — a claim that the
checkpoint had been written when nothing had. The caller re-derived the same
`operation_id`, the plan no longer matched the recorded one, and the write was
refused. A second project's sequence 320 had been doing this since 08-28.

**A drain whose cost was the backlog.** One cycle claimed and replayed the whole
queue and rewrote `state.json` three times while asking for the lock for 0.5 s.
The original outage was repaired on 08-29 and the system stayed stuck, because
the backlog was what prevented its own drain.

## Decision

**A batch is named after the whole batch.** `occurrence_id` is `batch:` followed
by the SHA-256 of the ordered `evidence_event_ids`, and the idempotency key is
that name plus the reason. An identical retry collapses as a duplicate, which is
what the old scheme intended; a different batch can never claim another batch's
reservation. Every element of the evidence list appears in the queue once and is
deleted on commit, so identical membership means one operation retried — the
warning against hashing a request body does not apply to a list of unique
identifiers.

**A spent reservation is retried, not reported as done.** A `reserved` checkpoint
whose transaction reached `discarded`, `aborted`, `conflicted` or `quarantined`
takes the next attempt number, exactly as
[[knowledge/notes/idempotent-retry-after-quarantine-decision]] already ruled for
quarantined rows. In-flight states — `preparing`, `prepared`, `applying`,
`aborting` — are deliberately excluded: those may still commit, and retrying one
would put a second writer on the same sequence.

**Recovery is bounded and unattended.** A drain cycle claims at most one window
of 100 from the head, so recovery is linear in the backlog and never impossible.
The nightly pass drains what the hook path cannot, with a lock budget of 10 s and
a total bound of 120 s, isolating each project so one bad row cannot stop the
rest.

## Consequences

The wedge class is unreachable going forward, and it is visible: `doctor`
reports `degraded` when a project's checkpoint sequence has not moved for an
hour, naming the sequence and the queue depth.

It orphaned the rows named under the old scheme. A quarantined or reserved
sequence is normally cleared by the original request arriving again under the
same name — `tests/test_project_journal.py` states that in seven tests — and
after the rename no request can produce those names. Three such rows were
holding 3 338 queued checkpoints.

That is repaired by one explicit, self-limiting pass,
`scripts/repair_orphaned_checkpoint_names.py`: an unsettled row whose
`occurrence_id` was not produced by the batch naming gets a fresh attempt
through the same step a returning request would have used. On a vault with no
orphaned names it does nothing. Recovery itself is unchanged, and so is the
rule that a quarantined head is the requester's to clear.

Stale reservations still never expire. A reservation that is never committed and
never abandoned blocks its name forever, and here that was indistinguishable
from a permanent wedge until the event bodies were diffed by hand. A bounded
reservation lifetime is a larger change and is not taken here.

One case is deliberately outside the spent set: a `reserved` row with no
transaction at all. It is what a reservation looks like after a prepare that
never happened, and equally what a pruned discarded transaction leaves behind
after thirty days — but it is also what a perfectly live reservation looks like
for the moment between `reserve` and `prepare`. Treating it as spent on the
absence of a transaction alone risks a second writer on the same sequence. Age
is the only thing that separates them, and `project_checkpoints` carries no
timestamp of its own.

## What it cost while it was being found

Two general designs were written and reverted, and the second did damage. A
rule that settled a reservation whose evidence looked already journaled rests
on evidence ids being unique per event; that holds for drain batches but the
schema does not promise it, and twelve tests caught the rule swallowing a real
write. Before it was reverted, hooks running against this vault marked
`llm-wiki` 1429 committed with nothing written, which left the journal ending
at 1428 while the database claimed 1429 — the exact fault
`ProjectJournalRebuildRequired` names, and for which no rebuild existed.
`scripts/repair_journal_gap.py` is that rebuild: from the journal head forward,
one sequence at a time, only where the database has a committed row the journal
lacks. See `docs/research/2026-08-30-a-sequence-nothing-can-settle.md`.

## Source / Evidence

- `docs/research/2026-08-30-a-batch-named-after-one-of-its-members.md`
- `docs/research/2026-08-30-a-backlog-that-prevents-its-own-drain.md`
- `scripts/integration_adapter.py` — `_batch_occurrence_id`,
  `PENDING_CLAIM_WINDOW`, `drain_pending_backlog`
- `scripts/markdown_transaction.py` — `_checkpoint_is_spent`,
  `_SPENT_TRANSACTION_STATES`
- `scripts/reclaim_runtime_state.py`, `scripts/doctor.py` — `_checkpoint_check`
- `scripts/repair_orphaned_checkpoint_names.py`, `scripts/repair_journal_gap.py`
- `tests/test_a_batch_is_named_after_the_batch.py`,
  `tests/test_a_backlog_drains_in_windows.py`,
  `tests/test_runtime_state_is_reclaimed.py`,
  `tests/test_orphaned_checkpoint_names_are_repaired.py`,
  `tests/test_a_journal_gap_is_repairable.py`
- `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, entry of 2026-08-30

## Links

- [[knowledge/notes/idempotent-retry-after-quarantine-decision]] — the rule this
  extends from quarantined rows to every spent attempt.
- [[knowledge/notes/reliable-memory-stage-2]]
- [[knowledge/notes/self-resolving-health-findings-decision]] — why the wedge is
  reported as a live condition rather than a permanent scar.
- [[knowledge/notes/observable-capture-and-bounded-maintenance-decision]]
