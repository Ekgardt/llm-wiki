# A retry must replay the same request

Dated 2026-09-13, after main went red on a Windows race that yesterday's fix
covered only half of.

## The facts

- `main` run 34760092169 (`b091561`): 52 jobs green, one red —
  `timing::windows_full::py3.13-s4`,
  `tests/test_blackboard.py::test_multiprocess_status_reads_remain_coherent_during_claim_and_complete`:

  ```
  completion of worker/3/task/3 failed after 2 of 12 attempt(s) in 58.67s;
  attempt 1: OperationalError('database is locked')
  attempt 2: OperationBoundElsewhereError('operation_id is already bound to a different request')
  ```

- Yesterday's fix
  (`docs/research/2026-09-12-a-lock-error-then-a-fence-error-is-a-question-not-a-retry.md`)
  handled the *other* second error, `blackboard claim resource epochs changed`,
  by reading back whether the completion had landed. Here nothing had landed, and
  the read-back correctly said so.
- The mechanism is not the problem. `markdown_transaction` already handles a
  retry of a bound operation: `_append_value_failure` sees
  "operation_id is already bound", `_settle_append_candidate` settles the bound
  transaction, and `_classify_settled_append` returns the committed record or
  advances a pending one. It raises `OperationBoundElsewhereError` for exactly one
  reason — **the block this attempt offers is not the block the operation is bound
  to**.
- And it is not, because `blackboard.complete_task` builds the record with
  `current = _utc_now(None)`: a fresh `completed_at` on every attempt. The
  operation id is stable (`blackboard-complete:<claim id>`), the payload is not,
  so a retry of the same completion is a different request by construction and can
  never be accepted.

## Practice on this date, and what it says

The sources for yesterday's note already said it in one line: **retry only what is
idempotent**, and optimistic concurrency requires re-reading rather than replaying
a stale request
([fix SQLite "database is locked" under concurrent writes](https://oneuptime.com/blog/post/2026-09-08-fix-sqlite-database-is-locked-concurrent-writes/view)).
An idempotency key covers a request only when the request is unchanged: this is
the standard contract of an idempotency key over a payload digest, and a caller
that mutates its payload between attempts has opted out of it. The mechanism in
this repository implements exactly that contract, and the defect is that one of
its callers breaks the contract with a clock.

## The decision

**`complete_task` takes the completion time as an argument, and a retry replays
it.** `completed_at` defaults to now for a first call, so nothing changes for the
CLI or any single-shot caller; a caller that retries after an unknown outcome
passes the value it used the first time, and the block is then byte-identical, so
the bound transaction is settled and adopted instead of refused.

- `blackboard.complete_task(project, claim, completed_at=None)`.
- The test harness captures one timestamp per claim before its first attempt and
  passes it to every attempt, which is what a real retry-aware caller must do.
- Nothing about the fence changes: `_load_live_claim` still refuses a claim whose
  epochs moved, and that refusal is still resolved by reading back whether the
  completion landed.

Why not the alternatives:

- **Drop `completed_at` from the record and take the time from the transaction
  log.** Truthful, and a bigger change: every reader of `completed.jsonl` would
  have to learn where the time now lives, for no gain over replaying the value.
- **Put a content digest in the operation id.** Then two attempts become two
  operations, and a pending first attempt could still commit after the second
  one, leaving two completion records for one claim.
- **Treat the refusal as contention in the harness.** It would hide the very
  incoherence the test exists to catch, and leave the product's retry hole open
  for real callers.

## What must be true after the change

- Two attempts at completing the same claim with the same `completed_at` end with
  one completion record and a `True` return, whatever order the lock let them run
  in.
- A first call that passes nothing still records the moment it ran.
- The multiprocess coherence test stops failing on this path, and the failure it
  was written for — a status read that sees a half-published completion — still
  fails it.

Files: `scripts/blackboard.py`, `tests/test_blackboard.py`,
`docs/research/2026-09-13-a-retry-must-replay-the-same-request.md`.
