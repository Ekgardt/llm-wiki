# Every budget inside its step, and a drain that did not finish stays a drain

Dated 2026-09-14. Item 0.6 of `docs/AUDIT-2026-09-14-2.md`, the small findings on my
own fixes of this morning. The research before the fix.

## What was found

1. **The episode step's margin.** `scheduled_nightly._episode_step` passes
   `--budget-seconds 240` and kills the step at 300 s. `episode_consolidation` checks
   the budget before each batch (`_BatchRun`, `_out_of_time`), and a batch is one
   `llm_client.call_llm` — 90 s by default (`DEFAULT_TIMEOUT_S`) — plus the daily-log
   write and the checkpoint. A batch started at 239 s can still be running at 329 s:
   the parent kills it between the write and the checkpoint, the case item 2.3 of the
   same audit shows ends in a duplicate block. The nightly's own rule is a margin of
   `STEP_START_MARGIN_SECONDS = 120` (repository refresh: budget + 120; prune: 180
   under 300; weekly prune: 1 080 under 1 200). The test that holds the rule,
   `test_every_step_that_prunes_gives_it_a_budget_below_its_kill_timeout`, checks only
   the two prune steps and only `budget < timeout`.
2. **`inference_threads.settle` clears the stop flag in `finally`**, also when a thread
   is still running after the timeout. That thread then passes its next safe point
   and keeps encoding while the interpreter finalizes — the abort (exit 134) the
   module exists to prevent (`docs/research/2026-09-14-no-model-running-at-exit.md`).
   The graph: `settle` ← `mcp_server._settle_inference`; `stopping` is read by
   `mcp_server._warmup_stage` and the retrieval worker.
3. **The worker's last attempt.** A capture worker's failure is recorded as
   `deferred`. A worker process that fails before it claims a task consumes no
   attempt; a task that runs out of attempts is a queue state (`dead`, or stuck
   `ready`, items 2.1 and 2.2), which the process boundary cannot see. Not changed
   here; the visibility of such tasks belongs to 2.1/2.2.
4. **The CLI error excerpt.** `ProviderExited` carries a redacted 500-character tail
   of what `claude -p` printed before exiting non-zero. In text mode the CLI prints
   its answer only on success; on failure stdout holds the error. The excerpt goes to
   stderr and local logs under the gitignored `logs/`, never to the repository. No
   change.

## Practice on this date

- A child's deadline must end before its parent's kill by at least the longest
  uninterruptible unit it may start — the rule this codebase names audit OPS-10, the
  same shape as Kubernetes' `terminationGracePeriodSeconds` covering the work a
  container finishes after `SIGTERM`
  ([Pod termination](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination)).
- `threading.Event` is a shared flag; clearing it releases every waiter on it
  ([threading.Event](https://docs.python.org/3/library/threading.html#event-objects)).

## The decision

- `EPISODE_BUDGET_SECONDS = 180`, and the step's kill timeout is written as
  `EPISODE_BUDGET_SECONDS + STEP_START_MARGIN_SECONDS` (300 s, unchanged).
- The test is widened to the class: **every** nightly and weekly step that passes
  `--budget-seconds` leaves at least `STEP_START_MARGIN_SECONDS` before its kill.
- `settle` clears `stopping` only when no inference thread is left running; a drain
  that did not finish keeps asking the rest to stop. The flag clears when the last
  such thread ends, so a later server in the same process still warms, as the
  existing test `test_a_thread_that_will_not_stop_is_named_and_the_next_server_still_warms`
  requires — once the stuck thread is gone rather than while it runs.

Files: `scripts/scheduled_nightly.py`, `scripts/inference_threads.py`,
`tests/test_a_prune_inside_its_step.py`, `tests/test_no_model_running_at_exit.py`,
`docs/research/2026-09-14-every-budget-inside-its-step.md`.
