# An impact Git child is never orphaned

Date: 2026-09-25. Audit item C-40 (docs/AUDIT-2026-09-25-full.md).

## Fact
- `impact_analysis._git` starts Git with `subprocess.Popen` and only then builds
  its kill timer from `_remaining(deadline)`. `_remaining` raises `TimeoutError`
  when the deadline has passed, and that line sits outside the `try/finally` that
  kills and waits for the child. A deadline that expires between the caller's
  last check and the spawn leaves the child running with its stdout pipe open,
  and never reaped.
- It is the only `threading.Timer(_remaining(...))` in `scripts/`.

## Source (fetched 2026-09-25)
Python documentation, `subprocess`, https://docs.python.org/3/library/subprocess.html,
`Popen.communicate`: "The child process is not killed if the timeout expires, so
in order to cleanup properly a well-behaved application should kill the child
process and finish communication". `Popen.wait`: "Wait for child process to
terminate. Set and return returncode attribute." Cleanup is the caller's job: kill,
then wait. (That an unwaited child remains a zombie until reaped is POSIX process
behaviour, not a claim of this page.)

## Decision
The deadline is checked before the child exists, and the timer's delay is the
time left clamped at zero, so nothing between spawn and the `try/finally` can
raise. A deadline that passed is a `TimeoutError` with no process started.

## Files
- scripts/impact_analysis.py
- tests/test_an_impact_git_child_is_never_orphaned.py
