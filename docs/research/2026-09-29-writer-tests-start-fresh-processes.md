# Writer contention tests start fresh processes

Research date: 2026-09-29. Qualified; tests only.

The bootstrap qualification run emitted four Python 3.12 warnings from
`test_concurrent_identical_append_converges_once_during_distinct_event_churn`:
its process pool forks a parent that already has threads. The related executor
matrix and distinct-event contention test also select the default process pool.
No actual deadlock is claimed. An added worker guard reproduced the unwanted
fork context in the original churn test before changing executor construction.

Use one local process-executor factory with the explicit spawn context, shared
by all three construction paths in this module. Keep thread tests as threads.
Both module-level workers verify that a child process is not using fork; the
guard does not reject threads running in the main process. Workers already
receive vault and runtime paths explicitly and can be imported by spawn.
No concurrency, fencing, exact-once convergence or retained-event assertion is
removed. Existing measured/resource-derived worker sizing and timeout helpers
remain unchanged. There is no product code change or new numeric restriction.

This applies the same choice researched earlier today in
`2026-09-29-blackboard-tests-start-fresh-processes.md`, based on three independent
primary sources: [Python multiprocessing](https://docs.python.org/3/library/multiprocessing.html),
[Linux fork](https://man7.org/linux/man-pages/man2/fork.2.html), and
[Apple's archived threading guide](https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/Multithreading/AboutThreads/AboutThreads.html).
The Apple source is historical platform guidance, corroborated by current Python
documentation, not a claim of new APIs. Python 3.12.3 is installed.

Alternatives remain warning suppression (leaves inherited locks), forkserver
(not the common Windows/macOS mechanism), or serial execution (loses contention
coverage). Spawn's interpreter startup cost is accepted to retain real concurrent
processes with independent thread state. Repository search found only this module
and the already-corrected blackboard tests constructing ProcessPoolExecutor;
this is not a claim that every other kind of process launch has been reviewed.

The worker guard reproduced the old context: one failed test and four warnings
in 0.31 seconds. After changing all three pool construction paths, the complete
automatic-writer module plus bootstrap publication, hook diagnostics and actual
Lizard/AST complexity checks passed 156 tests in 70.99 seconds, with
DeprecationWarning promoted to an error. There were no warnings, skips or
deselections in that run. Ruff passed. Existing thread and process contention,
committed-transaction counts and exact event preservation checks remain intact.

The replaced default-fork construction is removed, not retained as an alternate
harness. Evidence prefix: `logs/audit-2026-09-29-completed-repair-writer-spawn-`.
Only Linux/Python 3.12.3 execution is qualified here. Private progress/log updates
remain deferred while compilation holds its knowledge snapshot.
