# The adoption pass says what it skipped, and looks past it

Dated 2026-09-17. Finding C-F12 of the third audit (low, confirmed by reading). The research
before the fix.

## What was found

- The capture worker starts every pass by adopting intents that were published but never
  given a task (`capture_adoption`, `docs/research/2026-08-28-adopting-an-orphaned-intent.md`).
  The pass returns what it adopted and what it skipped, with a reason for each skip.
- `flush_memory._adopt_orphaned_intents` throws that result away and swallows every
  exception. An intent that can never be adopted — its file deleted, its digest changed — is
  therefore re-read on every pass for ever, and nothing anywhere says so.
- The queue hands the pass its 32 oldest candidates. Thirty-two intents that can never be
  adopted fill that window on every pass, and no newer orphan is ever reached.
- Suspected and not fixed here: an intent whose publisher died between the `pending`
  publication and `ready` has no sweeper at all, because adoption looks only at `ready` rows.
  The rows and the query are the queue's; this is listed for the owner.

## Practice on this date

- The pass is a polling relay over a transactional outbox, and the pattern names its own
  weakness: "The Message relay might publish a message more than once"
  ([Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html),
  fetched 2026-09-17) — which adoption already answers with a dedupe key. The other known
  weakness of a polling relay is the poison record at the head of the poll: a relay that
  always takes the oldest N must be able to step past records it cannot send, or report
  them, or both.
- The product's own rule for a swallowed failure is that it is written to the
  capture-failure trail (`capture_diagnostics`), never silent.

## The decision

- When a full window yields no adoption at all, the pass asks for a window twice as large and
  examines only the records it has not yet seen, up to eight times the ordinary window. A
  healthy vault never takes the second step.
- The worker records one failure line per pass that raised or skipped something for a reason
  that will not mend itself, of the kind `capture_adoption`, naming how many and the first
  reason. A skip that is a writer race (decided by the error's type, as everywhere else) is
  retried by the next pass and is not recorded. An intent that can
  never be adopted is a standing loss and stays visible until someone deals with it.

Files: `scripts/capture_adoption.py`, `scripts/flush_memory.py`,
`tests/test_the_adoption_pass_says_what_it_skipped.py`
