# No task is claimed only to be killed

Dated 2026-09-14. Item 4.3 of `docs/AUDIT-2026-09-14-2.md`, and the same shape in the
nightly queue step. The research before the fix.

## What was found

- `doctor._run_bounded_worker` (called by `_repair_queue_followups` during
  `doctor --repair`) runs `memory_queue.run_worker` with
  `max_seconds = min(1, remaining)`. `claim` increments `attempts` when it leases
  (`_take_task_lease`); the processor child gets whatever is left of the worker's
  deadline, and a child killed by that deadline is recorded as `worker_timeout` — a
  failed attempt with backoff. The queue's handlers (`_MANUAL_TASK_HANDLERS`: `query`,
  `flush`, `compile`) call a model; none finishes in a second. The audit reproduced a
  task losing an attempt to each manual repair. Eight such repairs make a task dead.
- The cap dates from `0ce2705` ("harden reliable memory recovery") and is not
  explained there. After unblocking capabilities, the worker was meant to drain what
  had been unblocked; within one second it can only claim and kill.
- Every worker has the same tail: `_drive_worker` claims while `now < deadline`, so a
  task claimed one second before the deadline is killed one second later and loses an
  attempt. The nightly queue step runs `memory_queue.py work` with the default
  `worker_max_seconds = 600` inside a step killed at 600 s
  (`scheduled_nightly._queue_step`), so the parent can also kill the worker itself
  before it releases its lease — the case item 0.6 fixed for budgeted steps, missed
  here because the step's flag is `--max-seconds`, not `--budget-seconds`.
- Provider calls are bounded at 90 s by default (`llm_client.DEFAULT_TIMEOUT_S`).
- The code graph: `run_worker` ← `doctor._run_bounded_worker`,
  `memory_queue._cli_work` (the nightly `work` step and the manual CLI);
  `_record_bounded_worker_run` ← `_repair_queue_followups` ← `_repair_derived_actions`
  ← `_run_repairs`.

## Practice on this date

- A consumer should not take a message it cannot finish within its visibility window:
  AWS's guidance is to set the visibility timeout to the processing time and extend or
  release rather than let it lapse, since a lapse counts as a receive toward
  `maxReceiveCount`
  ([SQS visibility timeout](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html)).
  The equivalent here is not to claim with less time left than a task needs.
- The OPS-10 rule already used in this codebase: a child's deadline ends a margin
  before its parent's kill (`STEP_START_MARGIN_SECONDS = 120`).

## The decision

- `run_worker` takes `min_claim_seconds` (default 0, so direct callers and tests keep
  their behaviour): it claims no new task when less than that remains.
  `WORKER_MIN_CLAIM_SECONDS = 120` — one provider call's 90 s plus the child's start and
  settle — is the default of the `work` command.
- `doctor --repair` no longer runs a queue worker. A one-second worker can only burn
  attempts; unblocked tasks are drained by the next session or nightly worker.
  `_run_bounded_worker`, `_record_bounded_worker_run`, `_worker_should_stop` and
  `_worker_state_root_matches` are removed.
- The nightly queue step passes `--max-seconds 480` (its 600 s kill minus the
  120 s margin), and the budget test covers `--max-seconds` as well as
  `--budget-seconds`.

Files: `scripts/memory_queue.py`, `scripts/doctor.py`, `scripts/scheduled_nightly.py`,
`tests/test_a_prune_inside_its_step.py`, `tests/test_no_task_is_claimed_to_be_killed.py`,
`docs/research/2026-09-14-no-task-is-claimed-to-be-killed.md`.
