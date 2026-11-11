# Blackboard race tests start fresh processes

Research date: 2026-09-29. Change limited to test execution.

The 133-test SQL review run produced six Python 3.12 warnings: the two blackboard
process pools forked a multithreaded test process. Running the two tests alone
with deprecation warnings promoted to errors passed, establishing that the
warning depends on surrounding process state, not just the test name. No actual
deadlock is claimed. Fork inheritance is the confirmed warning cause.

Choose an explicit `spawn` multiprocessing context for both pools. Workers already
receive vault paths explicitly and are module-level importable functions. Use
a barrier for the two competing resource claimants, replacing the arbitrary
two-second start delay. The barrier timeout reuses the suite's existing
`LONG_TIMEOUT` hang bound; two participants correspond to the two real contenders.
No product limit, runtime root, dependency or persistent process is introduced.

Alternatives: suppressing the warning leaves inheritance unchanged; a longer
delay still does not prove readiness; `forkserver` is POSIX-specific and is not
the common Windows/macOS path. Spawn costs interpreter startup and avoids
inheriting parent-only thread/lock state. Keep real concurrent workers, fenced
ownership and the original winner/coherence assertions.

Three independent primary sources checked today:

- [Python multiprocessing](https://docs.python.org/3/library/multiprocessing.html)
  documents spawn, the Python 3.12 fork warning, context compatibility and the
  changed Python 3.14 defaults. The inspected runtime is Python 3.12.3.
- [Linux fork manual](https://man7.org/linux/man-pages/man2/fork.2.html)
  explains the surviving thread and inherited lock state.
- [Apple threaded programming guide](https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/Multithreading/AboutThreads/AboutThreads.html)
  warns against continuing framework execution after fork without exec. This is
  archived 2014 guidance, used for the stable platform constraint; current Python
  documentation corroborates the spawn choice, not an assertion of new Apple API.

The current POSIX fork page was also searched, but opening it returned an internal
error; it is not counted as a successfully inspected source. These sources explain
the mechanism. Local tests must establish that the changed harness still checks
real resource ownership, and cannot qualify Windows or macOS execution here.

## Results

The new worker guard rejected both old fork-based tests (two failures, six
warnings). After selecting spawn and replacing the contender delay with a
barrier, 36 blackboard, release-retry and actual Lizard/AST complexity checks
passed in 50.89 seconds without warnings. Repeating the two process tests with
`DeprecationWarning` treated as an error passed in 4.98 seconds. Ruff passed.
The successful standalone pre-change run remains recorded separately; it is not
the reproducer. The guard checks the actual worker start method and prevents a
future return to inherited fork state even when the test parent happens to be
single-threaded. Original ownership and coherence assertions remain intact.

This does not prove every race interleaving: the status-reader test still uses
its existing workload and pacing, and the tests execute on Linux/Python 3.12.3.
Product process launch paths were not changed. The replaced two-second start
delay is removed; no second harness or new dependency was retained.

Evidence prefix: `logs/audit-2026-09-29-completed-repair-blackboard-spawn-`.
`red.txt` is the standalone non-reproducing run, `worker-guard-red.txt` contains
the reproduced unsafe context, `green.txt` the related checks, and
`strict-green.txt` the stricter repeat. Private progress/log updates remain
deferred until the active compiler releases its knowledge snapshot.
