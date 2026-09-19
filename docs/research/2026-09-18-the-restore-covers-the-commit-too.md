# The restore covers the commit, not only the work before it

Dated 2026-09-18. Finding Q-L11 of the third audit (Markdown transactions, project path).

Files: scripts/markdown_transaction.py,
tests/test_a_checkpoint_that_fails_at_the_commit_puts_its_files_back.py

## What was found

`_commit_project_operations` moves every project target *inside* the database transaction that
commits the checkpoint, "so a failure rewinds both together". It rewinds them together for a
failure in the body:

```python
with self._connect() as database, begin_immediate(
    database, before_commit=self._require_current_operation_active
):
    ...
    try:
        self._mutate_project_operations(...)
        self._commit_project_checkpoint(...)
    except BaseException:
        self._restore_inflight_operations(record.id, changed)
        raise
```

The `try` ends before the `with` does, and `before_commit` runs in the `with`'s exit. So the one
failure this design exists to survive — the caller's deadline or cancellation landing at the
moment of commit — rewinds the database and leaves `journal.md`, `state.md` and the sealed
segment holding their new bytes. The audit marked the consequence "recovery reconciles later",
and it does: the row is still `applying`, and `_reconcile_unapplied_operation` sees the targets
already at their after state and marks them applied. But between the failure and the next
recovery, the vault holds a checkpoint that the journal database says never happened, and the
caller was told the checkpoint failed.

The second half of the finding, `_discard_failed_preparation`, is **not a defect**. There the
order is: delete the `preparing` row, then remove the artifacts. If the delete's commit fails,
the row survives *and* its artifacts survive — which is the consistent pair, and the one
recovery knows how to finish. Removing the artifacts of a row that is still there would be the
bug, and the code does not do it.

## Practice on this date

The rule is that compensation is owed for every step that may have taken effect, which includes
the commit attempt itself. The Saga pattern states the obligation in those terms: a saga is a
sequence of transactions where "If a local transaction fails because it violates a business rule
then the saga executes a series of compensating transactions that undo the changes that were made
by the preceding local transactions"
([microservices.io, *Pattern: Saga*](https://microservices.io/patterns/data/saga.html)).
"Preceding" is the whole point: the compensation has to be reachable from wherever the failure
lands, not only from inside the block the author was thinking about.

## The decision

- The restore moves out of the `with`, so it covers the commit as well as the body:
  `_commit_project_operations` is a `try` around one call that takes the lock, mutates and
  commits, and its `except BaseException` restores what was changed.
- `self._local.mutation_database` is still cleared in a `finally` inside the lock, before the
  commit hook runs, so the restore always works through its own connections and never through a
  transaction that is being rolled back.
- **Compensation is not cancellable.** Moving the restore alone was not enough, and the reason is
  the same defect one level down: the restore writes through `_apply_operation`, which calls
  `_require_current_operation_active`, which asks the same cancellation that just fired. The undo
  cancelled itself and left the target at its after state. A new `_uncancellable()` scope installs
  an infinite deadline and no cancel callback for the duration of the compensation, and
  `_rollback_for_quarantine` — the other place that undoes applied operations, where a cancelled
  undo was silently recorded as "this one could not be rolled back" — takes the same scope.
- `_discard_failed_preparation` is left exactly as it is, for the reason above.
