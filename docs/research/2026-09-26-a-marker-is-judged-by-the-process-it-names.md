# A marker is judged by the process it names

Date: 2026-09-26. Audit 2026-09-26, finding C-12 (orphan maintenance marker judged by
PID only; doctor owner liveness by bare PID).

## What was wrong (facts, read in the code)

Since 2026-09-17 (`docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md`)
`run/maintenance.lock` and `run/compile.pid` record the owner's start identity beside
its PID, and every registry row carries `process_start_identity`. Four readers still
threw the identity away and asked only whether the number existed:

- `OwnershipRegistry._remove_orphan_marker` (an ownerless `maintenance.lock`): a reused
  PID kept the marker, and every nightly and weekly skipped as `owner_busy`.
- `doctor._live_owner` (maintenance, queue and writer owner rows): a reused PID counted
  as a live owner.
- `installed_memory_repair._require_process_absent` (offline adoption): a reused PID
  blocked adoption as a live legacy owner.
- `MarkdownCoordinator._writer_owner_reclaimable` and the v3 queue's
  `_delete_stale_source_fences`: a writer gate or source fence of a dead owner waited
  for its lease to expire.

## Sources

- Linux man-pages, proc_sys_kernel(5), fetched 2026-09-26 from
  https://man7.org/linux/man-pages/man5/proc_sys_kernel.5.html: "/proc/sys/kernel/pid_max
  … This file specifies the value at which PIDs wrap around (i.e., the value in this
  file is one greater than the maximum PID)." The default is 32768.
- Linux man-pages, proc_pid_stat(5), fetched 2026-09-26 from
  https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html: field (22) starttime,
  "The time the process started after system boot." — the part of the identity
  `process_liveness` records on Linux, with the boot id.

Conclusion (mine): a PID is a name the kernel hands out again; only the recorded start
identity says the process is the same one.

## Decision

Every reader of a stored owner that has an identity compares it:
`process_liveness.owner_alive(pid, identity)` (doctor, writer gate, v3 source fences),
and `_marker_owner_alive` / `_require_process_absent` for the marker files, which keep
their refusal when the probe itself is in doubt. A record with no identity (written
before 2026-09-17, or the v2 queue) keeps the PID probe it always had.

## Against recurrence

`tests/test_a_marker_is_judged_by_the_process_it_names.py` walks every script and lists
the functions that call a PID-only probe or test `process_start_identity(pid)` only
against `None`; a function not among the named exceptions (the probes' own wrappers,
a just-spawned child, readers of identity-less records) fails the test.

## Files

- `scripts/operational_ownership.py`
- `scripts/installed_memory_repair.py`
- `scripts/doctor.py`
- `scripts/markdown_transaction.py`
- `scripts/memory_queue.py`
- `tests/test_a_marker_is_judged_by_the_process_it_names.py`
