# The adopted queue settles its own attempts, and can let the dead go

Dated 2026-09-17. Findings Q-L1 and Q-L2 of the third audit (the adopted V3 queue).
The research before the fixes.

Files: scripts/memory_queue.py,
tests/test_the_adopted_queue_settles_its_own_attempt_limit.py,
tests/test_a_dead_task_can_be_purged_when_it_is_asked_for.py

## What was found

- Q-L1, confirmed by reading and by running. `claim(owner, max_attempts=N)` on the adopted
  queue honours the caller's number: it picks claimable rows with `attempts < N` and writes
  it into the lease. `fail(lease, failure, max_attempts=N)` refuses the same number through
  `_require_adopted_retry_policy`, raising `queue_api_not_adopted`, and
  `_failure_is_terminal` settles by `DEFAULTS.queue_max_attempts` regardless. One option,
  two answers. `run_worker(max_attempts=N)` hands the same number to both, so
  `memory_queue.py work --max-attempts 2` on an adopted vault claims a task and then cannot
  record its failure; the task waits out its 120 s lease instead. The README, USER-GUIDE and
  the reliable-memory plan all show `--max-attempts 8`, which is the default, so no
  documented command is affected today.
- Q-L2, confirmed by reading. `dead` is a terminal state, so `cancel` returns False for a
  dead task; `purge(include_dead=True)` is refused on the adopted queue by name; `redrive`
  inserts a replacement and leaves the dead parent in place. A dead task therefore never
  leaves an adopted queue, and by the `run/` deletion contract a retained queue task blocks
  deleting `run/` — permanently, after the first task exhausts its attempts. The CLI already
  documents the intended escape: `--include-dead`, "Also purge attempts-exhausted tasks;
  they are retained by default."
- The second half of Q-L2 is about `append_capture_link_resolution`, the only resolver for
  `capture_link_conflicted`, which has no production caller; it is dead code and is removed
  in the dead-code change of this round, not wired in.
- Corrupt rows interact with this. `_demote_corrupt_finished_tasks` moves a finished row
  whose payload no longer hashes to its record into `dead / payload_hash_mismatch`,
  precisely so it leaves the purge selection and becomes something `quarantine-corrupt`
  accepts. So "purge the dead" must not mean those rows.
- Code graph: `claim`/`fail` ← `_drive_worker` ← `run_worker` ← `_cli_work`, and the
  scheduled worker. `_ordinary_purge_plan` ← `purge` ← the module-level `purge` ← `_cli_purge`.

## Practice on this date

- A queue's attempt limit belongs to the queue, not to whoever happens to be consuming it.
  Amazon SQS states it as a queue attribute: "Use a **redrive policy** to specify the
  `maxReceiveCount`. The `maxReceiveCount` is the number of times a consumer can receive a
  message from a source queue before it is moved to a dead-letter queue."
  ([Using dead-letter queues in Amazon SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)).
  There is no per-receive override, because two consumers with different numbers would
  disagree about when a message is finished.
- The same document treats dead-lettered messages as removable evidence, not as a permanent
  fixture: messages can be moved out of a dead-letter queue by redrive, and a dead-letter
  queue has its own retention period.

## The decision

- Q-L1. The adopted queue settles its own attempt limit, which is what its own contract
  already says in two places out of three. `claim` now refuses a non-default `max_attempts`
  by the same named refusal `fail` uses, so the option cannot change which rows are
  claimable while the failure path insists on the settled number. The worker checks its
  policy once, before it claims anything, so an operator who passes a number the adopted
  queue does not take is told at the start rather than after the first failure. Silently
  ignoring the flag was rejected: an operator who asks for two attempts and gets eight has
  been told nothing.
- Q-L2. The adopted purge implements `include_dead`, selecting dead rows whose payload is
  still valid and leaving `payload_hash_mismatch` rows to `quarantine-corrupt`, which is the
  route the product already has for them. Dead work then leaves the queue through the same
  export, authorisation and receipt machinery as succeeded and cancelled work, and `run/`
  becomes deletable again after a deliberate operator action. Changing the deletion contract
  so that dead tasks stop blocking deletion was rejected: retention is the point of the
  state, and the operator asking for the purge is the evidence that it is no longer wanted.
