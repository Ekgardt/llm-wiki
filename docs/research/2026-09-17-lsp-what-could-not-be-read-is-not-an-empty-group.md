# What could not be read is not an empty group, and three neighbours of the same shape

Dated 2026-09-17. Findings K-B8, K-B9, K-B10 and K-B12 of the third audit. The research
before the fix. Four small defects in the LSP process lifecycle, each one a place where
evidence that was missing, stale or unbounded was treated as if it were settled.

Files: `scripts/lsp_process_tree.py`, `scripts/lsp_process.py`,
`tests/test_an_unread_proc_entry_never_calls_a_group_inert.py`,
`tests/test_lsp_process.py`.

## What was found

- **K-B8, reproduced.** `_proc_entry_verdict` (`lsp_process_tree.py`) returned `None` both
  for "this entry is not in the group, keep scanning" and for "this entry could not be
  read". `_scan_proc_entries` reads `None` as "keep scanning", so a scan in which every
  entry was unreadable ended in `True` — the group is inert — immediately after
  `killpg(group, 0)` had succeeded, which proves the group exists. The docstring already
  said the unreadable case "maps to None", so the loss was known and written down.
  `_proc_stat_payload` is careful here: a process that vanished between `scandir` and the
  read answers `b""`, which is proof that it left, and only a real `OSError` answers `None`.
- **K-B9, reproduced.** `_is_descriptor_path` was `name.startswith("/proc/self/fd/")` and
  `_is_inherited_descriptor` took the last `/`-separated segment. So
  `/proc/self/fd/../../../tmp/evil/7` with `pass_fds=(7,)` passed both checks, and
  `_resolved_executable` returns a descriptor path unresolved, so `os.stat` follows it to
  `/tmp/evil/7`. The input comes from the in-code launch guard, so this is a hardening gap
  and not a live hole; it is still the only check standing between a launch argument and
  an arbitrary file.
- **K-B10.** A terminal cleanup that cannot finish left `_RecoveryState.pending` set, and
  `_recovery_wait` waited a flat 50 ms before trying again — twenty passes a second, each
  one a `killpg` and an `fsync`, for the life of the process.
- **K-B12, half confirmed.** `_attempt_lsp_request` and `_notify_lsp_process` read
  `generation.protocol` and `generation.process` *after* `_request_generation` released the
  lifecycle lock, guarded by `assert protocol is not None and process is not None`.
  Cleanup sets both to `None` from another thread (`lsp_process.py`,
  `_finish_generation_protocol` and `_forget_released_process`), so a retire landing in
  that window ends in `AssertionError` — `AttributeError` under `-O` — instead of the
  retry path the code has for exactly this case.

## Practice on this date

- Why an unreadable `/proc` entry may not be passed over: the fields the scan reads are
  "pgrp %d (5) The process group ID of the process." and "session %d (6) The session ID of
  the process." (`proc_pid_stat(5)`,
  <https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html>, fetched today). An entry
  whose `stat` could not be read has neither field, so it is not evidence of anything —
  least of all of absence.
- The second half of K-B12 — "`lsp_process_tree` signals a group id without re-verifying
  ownership (needs pid wrap)" — **is not a defect.** POSIX: "A process ID shall not be
  reused by the system until the process lifetime ends. In addition, if there exists a
  process group whose process group ID is equal to that process ID, the process ID shall
  not be reused by the system until the process group lifetime ends", and "A process group
  ID shall not be reused by the system until the process group lifetime ends"
  (Base Specifications Issue 7, XBD 4.14 Process ID Reuse,
  <https://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap04.html>, fetched
  today). A process group lifetime "ends when the last remaining process in the group
  leaves the group" (XBD 3.296, chap03, fetched today). So while the group still holds a
  process the id cannot name anything else, and once it holds none, `killpg` answers
  `ESRCH` and `_observe_posix_tree` reports the group absent. There is no window to close
  and no POSIX primitive that would close one.

## The decision

1. `_scan_proc_entries` asks `_proc_entry_keeps_group` directly. `False` — read, and not
   in the group — is the only answer it passes over; anything else ends the scan through
   `_scan_verdict`, which keeps "unreadable" unreadable. The caller already reads the
   unknown answer as "not inert", so the failure stays closed.
2. A descriptor path is `<root><decimal digits>` and nothing else. `_descriptor_number`
   returns the number or `None`, and both the path test and the inherited-descriptor test
   are that one function. `[0-9]+` rather than `str.isdecimal`, which accepts digits from
   other scripts that `int()` then happily parses.
3. The recovery retry beat doubles from 50 ms to a 2 s ceiling while the same work stays
   owing, and returns to 50 ms as soon as nothing is owed. A cleanup that is stuck is
   stuck; past a couple of seconds the retries cost more than they can win back.
4. `_request_generation` returns the generation together with the two ends of its channel,
   taken under the lifecycle lock that already proved they were there. A retire landing
   afterwards then ends in the protocol's own `ProtocolViolation`, which is the retry path,
   instead of in a broken invariant. The `assert` statements are gone with the race they
   were watching.

Rejected: reporting an unreadable `/proc` entry as "in the group" (it would make every
`EACCES` a hang); a re-verification of the process group before each signal (there is
nothing to verify against, see above).
