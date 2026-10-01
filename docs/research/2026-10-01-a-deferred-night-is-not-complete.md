# A deferred night is not complete

Research date: 2026-10-01. Installed Linux systemd 255.4, Python 3.14.6.

The native nightly reached its existing 1800-second compile-following bound,
skipped lint/index/graph, and recorded success. The compiler still ran in its
oneshot unit's cgroup; ending the unit terminated it. Its last state remained
running. A previous deferred run was examined only after a new compile could
overwrite the start stamp, so that unfinished outcome could disappear.

Sources read on the research date:

- [systemd v255 run implementation](https://github.com/systemd/systemd/blob/v255/src/run/run.c)
  and [kill contract](https://github.com/systemd/systemd/blob/v255/man/systemd.kill.xml):
  a session is not an independent service lifecycle; scope execution migrates its
  own PID and then execs the command. A separate managed scope is a candidate for
  preserving the existing PID/identity and environment contract.
- [Linux cgroup v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html):
  grouping and process migration are independent of process parentage.
- [Python signals](https://docs.python.org/3/library/signal.html): SIGINT normally
  raises KeyboardInterrupt, while SIGTERM does not automatically become a Python
  exception caught by the compiler's existing BaseException finalization.

Alternatives: longer wait, disabling cgroup cleanup, a separate managed compiler,
or cooperative batch-boundary deferral. Longer waits do not repair ownership;
disabling KillMode leaves unmanaged work. Independent ownership still requires
native qualification and is not claimed implemented by this status correction.

This change preserves the existing wait and scheduling bounds. A pending compile
marker yields deferred maintenance, never a new successful completion timestamp.
Real errors still take precedence. Doctor and session context name the deferred
work. A live compiler is not declared lost. An old dead compiler's missing outcome
is counted before the next compile can replace the start stamp; the established
marker and state schema are retained. Once compilation is complete, the ordinary
post-compile pass can record success. Nothing deletes failed attempts or journals.

Four regressions distinguish old success/lost-live-run/overwritten-loss/unknown-
doctor behavior; a fifth distinguishes the empty session-context warning.
This is a partial delivery: it makes the existing lifecycle truthful, but does
not itself let a compiler outlive its nightly cgroup. Native ownership proof was
blocked by user-bus access in the available sandbox. Full compiler/nightly
acceptance and that ownership correction remain open; no timeout is enlarged.

## Follow-up: finish the owed pass before starting new inputs

The ownership limitation above was subsequently resolved and qualified through
the actual Linux service: the compiler survived parent exit and completed its
saved inputs. This exposed a second ordering defect. The next nightly discarded
the deferred marker, started new work, and could defer the same post-compile
steps again. Continuous new capture could therefore keep lint and maintenance
waiting even after the earlier compiler succeeded.

Additional primary research checked on 2026-10-01:

- [systemd v255 timer contract](https://github.com/systemd/systemd/blob/v255/man/systemd.timer.xml):
  a timer activates a service; it does not know the application's saved stage.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): an update's
  committed state is the durable boundary. The existing transactional state
  updater remains the authority for completing this obligation.
- [Temporal workflow execution](https://docs.temporal.io/workflow-execution):
  recovery resumes from recorded progress and distinguishes open work from a
  closed outcome. This informs ordering only; no workflow dependency is added.

When a deferred marker exists and the current compiler has a recorded terminal
outcome and is no longer running, the existing nightly entry now resumes its
post-compile pass before taking new inputs. The completed compiler's error, if
any, is reported. If another compile superseded the recorded start, maintenance
can still check current published data, but the missing original outcome is
reported as a failure, never inferred successful.

The marker remains until all post-compile steps succeed. Exceptions and returned
failures leave it available for a later retry. Its removal compares the original
marker inside the state update, so a concurrent replacement is not erased.
Existing saved string markers and compile states need no migration. The ordinary
scheduler fence and heartbeat still protect the entire pass; live compilers are
not interrupted. Pending capture is retained for its ordinary subsequent pass.

Alternatives rejected: manually clearing the marker loses the obligation;
raising the wait bound cannot prevent repeated deferral; running a second
compiler duplicates work; adding another daemon or scheduler is unnecessary.
No time, data, retry, or model limits change. The new regression fails on the old
entry because it takes new inputs before the owed tail; related tests cover
failed compilation, post-step retry, exceptions, and concurrent marker changes.
Native continuation of the saved production pass remains a separate acceptance
step from these tests.

## The health snapshot follows the terminal state

Follow-up on 2026-10-01: a successful continuation wrote its health snapshot
before its `finally` clause committed the terminal scheduler outcome. Session
start reused that snapshot for its existing 36-hour freshness window, so it
reported the previous failed night beside the new successful result.

The complete path is `_post_compile_pass` → `_write_health_report` →
`_run_nightly_body` finalization → `_record_nightly_result`, followed by
`session_start_context._stored_health_report` at the next session. The first
snapshot was necessarily earlier than the state it purported to summarize.
Move the single measurement to immediately after terminal-state recording.
Successful, failed, deferred and exception outcomes are measured from stored
state, without patching a check to a hoped-for success. The existing health
budget remains; a failed report still cannot change the work outcome.

Primary references checked on 2026-10-01:

- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): consumers
  read committed state, not a prediction of a later transaction.
- [Python 3.14 finally semantics](https://docs.python.org/3.14/reference/compound_stmts.html#finally-clause):
  finalization runs on return and on exception. The same structure remains
  supported by the project's Python 3.10–3.14 versions.
- [systemd v255 service contract](https://raw.githubusercontent.com/systemd/systemd/v255/man/systemd.service.xml):
  the service manager observes process completion; application health remains
  the application's recorded result. No service override is needed.

Alternatives rejected: measuring twice adds a second full scan; manually
rewriting only the scheduler check can conceal a real failure; lowering the
cache lifetime makes every session pay for an ordering defect. The report
continues to be a dated snapshot and does not promise future health. No new
schema, limit, dependency or daemon is introduced. Existing saved reports are
compatible and replaced by the next legitimate health measurement.

Regression tests run the producer and the existing cached-report reader. On
the old code, the stored report observes the previous status, and deferred or
exception paths leave no new report. The new code observes the committed
success, failure, deferral or exception outcome exactly once.
