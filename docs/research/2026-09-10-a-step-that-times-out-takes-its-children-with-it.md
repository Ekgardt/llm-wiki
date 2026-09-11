# A step that times out takes its children with it

Date: 2026-09-10. Trigger: audit finding OPS-06. The nightly and weekly
passes run each step through `maintenance_helpers.run_step`, which calls
`subprocess.run(cmd, timeout=...)`. On `TimeoutExpired` Python kills the
direct child only; a step that spawned its own workers — `memory_queue.py
work` (processor subprocesses), `repository_index.py refresh-all`,
`install_models.py` — leaves them running and writing to the vault while
the pass logs "TIMEOUT — skipping, continuing" and moves on.

## Sources

1. Python `subprocess` reference, `run()`: "If the timeout expires, the
   child process will be killed and waited for." — the child, not its
   descendants. https://docs.python.org/3/library/subprocess.html#subprocess.run
2. Same reference, `Popen(start_new_session=True)`: `setsid()` in the child,
   so the whole tree shares one session and process group, and
   `os.killpg(os.getpgid(pid), SIGKILL)` reaches every member that has not
   left the group. On Windows, `CREATE_NEW_PROCESS_GROUP` plus
   `taskkill /PID <pid> /T /F` ends the tree.
   https://docs.python.org/3/library/subprocess.html#subprocess.Popen
3. This repository: `sync_memory._run_process_tree` already does exactly
   that — new session or process group, `killpg` or `taskkill /T`, a bounded
   cleanup wait, and `ProcessTreeTimeout(subprocess.TimeoutExpired)` carrying
   the cleanup result — and is used by the sync and the install smoke test;
   the nightly runner never used it.

## Decision

`maintenance_helpers._run_to_files` runs the step through
`sync_memory._run_process_tree` with the same on-disk streams; a timeout
still returns 2 and the log line now says the tree was ended, naming a
cleanup error when the kill itself failed. A test spawns a step with a
grandchild and proves the grandchild is gone after the bound.

Files: `scripts/maintenance_helpers.py`, `tests/test_maintenance_helpers.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-operations-and-reliability.md`.
