# A redelivery after midnight is recognised

Dated 2026-09-14. Item 2.10 of `docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- `daily_log_append.append_daily` writes to today's file
  (`knowledge/daily/<datetime.now() date>.md`). With an `operation_id` it goes through
  `locked_append_once`, which looks for the operation's marker comment
  (`<!-- llm-wiki-operation:<sha256(operation_id)> -->`) **in today's file only**, then
  calls `append_knowledge(operation_id, today's file, block)`.
- An event first appended at 23:59 is in yesterday's file. Delivered again after
  midnight — queue delivery is at least once (`CLAUDE.md`, Stage 2 contract), and the
  queue retries with backoff up to an hour — the marker is not found in today's file,
  and the transaction layer finds the operation id already committed for another
  target: `OperationBoundElsewhereError` ("operation_id is already bound to a different
  request", `markdown_transaction._classify_settled_append`).
- `capture_diagnostics` classifies `OperationBoundElsewhereError` as contention, so the
  task is retried as a race and fails the same way every time until its attempts run
  out. Found by reading the code; not reproduced on the live vault.
- The code graph: `locked_append_once` ← `append_daily` ← capture, flush, episode
  consolidation and the adapter's daily writers; `_unappended_marker` ←
  `locked_append_once` only.

## Practice on this date

- An idempotent consumer recognises a duplicate by its key wherever the first delivery
  landed, not only in the partition the redelivery would have chosen
  ([microservices.io, Idempotent Consumer](https://microservices.io/patterns/communication-style/idempotent-consumer.html)).
- Day-partitioned logs make the partition a function of the delivery time; the lookup
  has to cover every partition a retry window can span.

## The decision

- `_unappended_marker` looks for the marker in today's file and in the previous day's
  file. The queue's retry window (backoff capped at an hour, eight attempts) is far
  shorter than a day, so a redelivery always lands within one midnight of the first
  write. A redrive days later is not covered; it is an operator action on a dead
  task, whose first attempt did not commit.

Files: `scripts/daily_log_append.py`,
`tests/test_a_redelivery_after_midnight_is_recognised.py`,
`docs/research/2026-09-14-a-redelivery-after-midnight-is-recognised.md`.
