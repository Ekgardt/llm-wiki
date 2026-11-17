# An owner does not need access to PID 1

Date: 2026-09-30. Status: corrected and tested in the isolated candidate;
host-wide regression and installation remain pending.

## Host evidence and root cause

The owner's full pytest invocation did not execute the suite: collection ended
with two errors in 5.81 seconds. Both traced through `process_start_identity`
to a permission refusal reading `/proc/1/ns/pid`. The proposed Linux scope check
incorrectly required an ordinary user to inspect a root-owned process's namespace.
The original host log and JUnit report are retained as
`logs/audit-2026-09-30-host-collection-failure.{log,xml}`.

This was a candidate compatibility defect, not a reason to elevate privileges,
skip ownership tests, or treat an inaccessible namespace as dead. The native
index was verified fresh before reviewing its scope/identity graph. The graph
declares incomplete coverage; source inspection covered the shared reader,
ownership consumers, and the tests that construct identities during collection.

## Correction and sources

The candidate now reads its own `/proc/self/status` and requires the `NStgid`
field to contain exactly its own `getpid()` value. The kernel prints this list
from the namespace associated with the procfs mount through the task's active
PID namespace. One matching value establishes common PID coordinates. More
than one value means an ancestor procfs view, even if the numbers happen to
be equal. Missing, malformed or unreadable evidence does not authorize probing
a foreign PID as if it were local. The existing own-namespace device/inode is
still read from `/proc/self/ns/pid`; PID 1 is no longer inspected.

Primary sources checked on 2026-09-30:

- [Linux proc status manual](https://man7.org/linux/man-pages/man5/proc_pid_status.5.html)
  specifies NStgid ordering and its availability since Linux 4.1.
- [Linux 6.8 task status implementation](https://raw.githubusercontent.com/torvalds/linux/v6.8/fs/proc/array.c)
  prints NStgid by iterating from `ns->level` through `pid->level`, under
  `CONFIG_PID_NS`; this independently verifies the interface interpretation.
- [Python 3.12 process identity API](https://docs.python.org/3.12/library/os.html#os.getpid)
  supplies the current process ID used for comparison.

Observed execution remains Linux 6.8.0-139-generic and Python 3.12.3. No upgrade
or new dependency was made. Kernels without the required namespace evidence
remain unqualified and fail conservatively. The status file is streamed to the
required field without imposing a new arbitrary whole-file size limit: other
fields, such as supplementary groups, can legitimately be large.

Alternatives rejected: requiring privilege to read PID 1; ignoring its permission
error; comparing only a numeric `/proc/self` link, which cannot distinguish
coincident numeric PIDs in nested namespaces; or assuming a missing NStgid
means a matching namespace. The persisted identity format, host/boot continuity,
time-namespace check, runtime layout and migration requirements are unchanged.
The cost is one streamed self-status read instead of statting PID 1. No LLM call,
retry, delay, or new operational limit was introduced.

## Qualification

Seven regression cases failed before the correction; they cover PID-1 permission
denial, nested coordinates including equal numbers, and missing/invalid evidence.
One existing dispatch test substitutes PID 77; it now supplies matching simulated
procfs coordinates too, rather than mixing a fake PID with real process metadata.
The full related group, including actual CCN/branch-shape checks, passed
**128 tests in 33.66 seconds**. Ruff passed for all three changed files.

A real two-command probe recorded a live process in one execution namespace.
The observer in another namespace answered `unknown` and retained it. The owner
then acknowledged input and exited normally. The change preserves protection
against the original hidden-process defect. Actual host permission compatibility
and the full suite still require the owner's repeat run.

The three-file incremental patch applies to the existing isolated candidate,
not directly to the installed old reader. Before/after hashes and evidence are
under `logs/audit-2026-09-30-proc-self-*`. The obsolete PID-1 comparison was removed
from the candidate; there is no parallel fallback implementation. Production
code, old-reader migration, private progress/log updates, production index and
post-install cleanup remain pending. This is not certification that the entire
audit or all nine laws at release level have been completed.
