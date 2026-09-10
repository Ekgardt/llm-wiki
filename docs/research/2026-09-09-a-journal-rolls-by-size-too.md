# A journal rolls by size too — 2026-09-09

## What broke

The nightly compile has failed since 2026-09-07 with `claim tree page
exceeds 4194304 bytes`. The page is `knowledge/projects/no-hands/journal.md`:
4.2 MB, 981 checkpoint events of about 4.3 KB each. The journal rolls into
a sealed segment only at `MAX_JOURNAL_EVENTS = 1000`, and the claim tree
refuses any page over `MAX_CLAIM_TREE_FILE_BYTES = 4 MB`, so a journal of
fat events crosses the claim tree's cap nineteen events before it would
roll. Two bounds, set apart, disagreeing.

## What every append-only log does

Kafka rolls a segment when either `segment.bytes` or `segment.ms` is
reached, whichever first (https://kafka.apache.org/documentation/#brokerconfigs_log.segment.bytes);
event-sourced stores snapshot the projection and start a new log segment
so the fold of the live segment equals the fold of everything sealed
(Fowler, https://martinfowler.com/eaaDev/EventSourcing.html). Our journal
already seals segments and writes the snapshot event; it only lacked the
size trigger.

## Decision

- The journal rolls at `ROTATE_ABOVE_BYTES = 2 MB` or at 1000 events,
  whichever comes first; the sealed segment is named by its sequence
  range as before, and the claim tree never sees it (`PROJECT_CLAIM_FILES`
  names `journal.md` only).
- The claim tree's page cap rises to 8 MB, the ceiling
  `project_journal.MAX_JOURNAL_BYTES` already allows, so a page the journal
  accepts is never one the claim tree refuses — and the live 4.2 MB journal
  compiles tonight, before its next append rolls it.
- The stale `.journal.md.<hex>.tmp` beside it (835 KB, 2026-09-07) is a
  transaction's abandoned temporary file, ignored by git; reclaim should
  remove such files, and does not yet — noted, not fixed here.
