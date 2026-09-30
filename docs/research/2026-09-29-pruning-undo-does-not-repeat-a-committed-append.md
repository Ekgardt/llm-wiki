# Pruning undo images must not duplicate a committed append

Research date: 2026-09-29. This is a correction inside the existing transaction
API, with no new database, directory, dependency or retention period.

The delayed-replay review found that `_nothing_to_compare` treats pruned undo
images as a reason to advance to another append attempt. The transaction row
and its before/after hashes remain, and the journal may still contain the exact
committed append. In that case advancing repeats an already delivered event.
The existing regression only covers a file deliberately removed after pruning;
that recreation behavior must continue to work.

Three independent primary sources were rechecked:

- [AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
  requires consumers to handle duplicate delivery. Retaining a message alone
  does not prove its side effect is idempotent.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html) explains the
  persistence assumptions behind the committed row. This project retains its
  rollback-journal/FULL/local-filesystem contract.
- [Git objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects) illustrates
  content-addressed integrity. No Git operation or Git object format is added.

Selected correction: compare the requested block against the current journal
and the committed operation's retained before/after hashes. A matching prefix
before the block and prefix through the block prove the original append even
when later entries follow it. A text match alone is insufficient. Repeated and
overlapping equal text must be considered; incrementally hash candidate prefixes
instead of repeatedly hashing the whole prefix. Only committed, single-target
operations can supply this proof. An existing text occurrence with conflicting
hash evidence refuses reuse; it must not be silently duplicated as a fallback.

Keeping every undo image forever conflicts with the established undo cleanup
contract and increases disk cost. Adding a second append ledger duplicates
retained transaction evidence. Assuming every matching line means the same
event confuses identical distinct occurrences. Increasing an arbitrary retry
window does not correct the decision to append twice.

The proof is a project-specific inference from its retained transaction fields,
not a correctness claim supplied by those sources. Missing or changed journals
and archived journals require separate treatment. The existing removed-file
recreation case stays tested; this correction alone does not qualify archived
breadcrumb replay or terminal cleanup. Python 3.10-compatible standard-library
hashing is sufficient. There is no new logical input or attempt limit.

Qualification begins with actual committed appends, normal three-day pruning,
and a delayed request for the same operation. Test both creates and replaces,
later journal growth, overlapping equal text, distinct equal occurrences, and
conflicting prefix evidence. Run the existing append/transaction regressions
and actual complexity analysis before accepting the correction.

The original implementation failed four delayed-replay cases while the existing
removed-file recreation case passed. After correction, 25 focused append tests
passed. The extended transaction/recovery/redrive/abort/complexity run passed
215 tests with five platform-dependent skips. Equal distinct appends remain
distinct; overlapping matching text is resolved by before/after hash evidence;
an altered prefix refuses reuse and remains unchanged. Archived-journal replay
is still outside this demonstrated result.
