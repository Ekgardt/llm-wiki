# A committed transaction is not a failure

Date: 2026-09-11. Trigger: CI run 34655557302 (PR #31) was red on one job
only — `timing::windows_full::py3.10-s2` —
`tests/test_project_journal.py::test_concurrent_duplicate_replay_creates_one_forward_attempt`
raised `ProjectFenceError: project lease changed before checkpoint apply`.
Linux and macOS were green. The same test has passed on Windows before, so
this is a race, not a break.

## What the log says

From the job log of 2026-09-11 23:14:35 UTC:

* The failing thread (`agent-c`) held a live lease: token `3fcfc644…`,
  fencing epoch 2, `expires_at` `2026-09-11T23:12:27Z` — the refreshed row
  it read back carried the same token and epoch.
* The raise came from `markdown_transaction._prepared_preconditions`:
  `project lease refresh requires a prepared transaction`, code
  `precondition_failed`, for transaction `1cc294397b95…`.
* `project_journal._checkpoint_under_lease` then quarantined the attempt and
  re-raised it as `ProjectFenceError`.

So the lease was fine. What was not `prepared` was the transaction — because
the other thread (`agent-b`) had already committed it.

## The window

`_checkpoint_under_lease` returns a duplicate receipt only when the
reservation row says `state == "committed"`:

```
reserved, duplicate = self._reserve(slug, event, lease)
if duplicate and reserved.state == "committed":
    return self._receipt(reserved, duplicate=True)
```

The row and the transaction are not updated in the same instant. A second
caller that reserves the same occurrence while the first one's transaction is
already committed but the row still reads `reserved` falls through to
`_apply_reserved`, asks to refresh the lease precondition of a transaction
that is no longer `prepared`, and is told `precondition_failed`. The
checkpoint attempt is then marked `quarantined` — a permanent record saying
the work never happened — although the work did happen, in full, moments
earlier, and the caller's own event is durably in the journal.

`_checkpoint_is_spent` already reasons this way in the other direction: a
`reserved` row whose transaction reached a terminal state *other than*
committed is retried rather than reported as a duplicate
(`docs/research/2026-08-30-a-batch-named-after-one-of-its-members.md`). The
committed case was left to the row alone, and the row lags.

Windows makes it visible because its filesystem and timer granularity widen
every window in this test; the defect is not Windows-specific.

## Decision

`precondition_failed` is graded against the transaction, not against the row:

* If the reservation's transaction is `committed`, the caller receives the
  ordinary duplicate receipt for that sequence. Nothing is quarantined,
  because nothing failed: this is the idempotent replay the checkpoint API
  promises.
* Otherwise the behaviour is unchanged — the attempt is quarantined and
  `ProjectFenceError` is raised, because the work really did not happen.

One new reader on the coordinator, `transaction_state(transaction_id)`,
returns the state or `None`; it is a bounded single-row read on the primary
key and adds no lock. Nothing else moves: no schema change, no new state, no
retry loop, no sleep.

The regression test does not race. It drives the exact state the log
recorded — a committed transaction behind a row still reading `reserved` —
and asserts the second caller gets a duplicate receipt and that no attempt is
quarantined.

Files: `scripts/project_journal.py`, `scripts/markdown_transaction.py`,
`tests/test_project_journal.py`.
