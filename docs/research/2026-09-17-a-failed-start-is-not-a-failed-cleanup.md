# A failed start is not a failed cleanup

Dated 2026-09-17. Findings M6, M7 and L21 of the third audit (queue slice, the worker's
child process). The research before the fixes.

Files: scripts/memory_queue.py,
tests/test_a_child_that_dies_at_start_does_not_halt_the_worker.py,
tests/test_a_blocked_task_has_a_way_back.py, docs/USER-GUIDE.md

## What was found

- M6. `_run_processor_child` tracks the child's descendants only after the ready handshake.
  Its `finally` passes `run.tracked_descendants` straight to `_terminate_processor_child`.
  Before the handshake that value is `None`. `_ChildRun.stop` documents `None` as "never
  tracked, discover the tree now"; the `finally` passes it as an explicit value, which
  `_await_cleanup` reads as "unknown tree" and answers False. The raise inside `finally`
  replaces the real error with `process_cleanup_failed`, the worker blocks the task with
  `blocked_capability="process_cleanup"` and halts. Reproduced with a processor whose
  unpickling exits the spawned child: the code reported was `process_cleanup_failed`, not
  `processor_result_malformed`.
- M7. `process_cleanup` is the only capability the queue ever blocks on. Nothing moves such
  a task again: `redrive` answers `redrive_requires_dead`, and the doctor's unblock handles
  only `llm.*` capabilities that nothing emits. `cancel` is the only exit, and it loses
  the work.
- L21. `_processor_result` re-reads the clock after the processor returned and records a
  finished success as `worker_timeout` when the deadline passed in between. For a `query`
  the published answer is lost and the work repeated.
- Code graph: `_run_processor_child` ← `_interruptible` / `run_worker`'s default runner;
  `_terminate_processor_child` ← `_ChildRun.stop`, `_run_processor_child`;
  `_processor_result` ← `_process_lease`.

## Practice on this date

- Python documents what a dead child looks like to its parent: "The child's exit code. This
  will be `None` if the process has not yet terminated. ... If the child terminated due to
  an exception not caught within `run()`, the exit code will be 1. If it was terminated by
  signal N, the exit code will be the negative value -N."
  ([multiprocessing.Process.exitcode](https://docs.python.org/3/library/multiprocessing.html#multiprocessing.Process.exitcode)).
  A child that exited on its own before the handshake is a known state, not an unknown
  one: on POSIX its process group can be probed, and a group that is gone is a clean tree.
- A deadline bounds work that is still running. Once the result is in hand, discarding it
  because the clock moved while it was being returned repeats the cost and gains nothing.

## The decision

- M6: the `finally` of `_run_processor_child` stops the child through `_ChildRun.stop`, the
  one place that already knows that "never tracked" means "discover now". Windows keeps its
  fail-closed answer for a child that is already gone, because it cannot enumerate that
  child's tree afterwards; that is unchanged and cannot be tested here.
- M7: the adopted queue gains `unblock(task_id)`: a `blocked` task goes back to `ready`
  with its capability and error cleared, under `begin_immediate`, refusing any other
  state with `unblock_requires_blocked`. The CLI gains `unblock <task_id>`. It is an
  operator's statement that the capability is back — for `process_cleanup`, that the
  leftover processes are gone. No MCP tool is added.
- L21 is left as it is. `test_worker_times_out_handler_without_result_or_live_lease` pins
  the opposite on purpose: a result that arrives at or after the deadline is a late result
  and is discarded, so the lease is never settled past its bound. Keeping a success that
  landed late would break that stated rule; it is the owner's to change, not a defect.
