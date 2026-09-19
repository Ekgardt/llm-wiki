# The child's tree is read when the task is done, not when the child starts

Dated 2026-09-18. Finding Q-L20 of the third audit (operational core, worker).
Outcome: **not a defect in the form it was stated**; the docstrings that made it read like one
are corrected, and the macOS half is decided and recorded.

Files: scripts/memory_queue.py,
tests/test_a_childs_tree_is_read_when_it_is_killed.py

## What the finding said, and what the code does

The audit recorded: "Descendant tracking: macOS returns `[]` ('none'), Windows None
('unknown'); Windows snapshot is taken at handshake, so always empty."

The second half does not hold, and the reason is worth writing down because the code went out of
its way to hide it. `_processor_child_entry` runs

```python
_send_processor_frame(sender, _processor_result_frame(processor, task))
```

Python evaluates the argument first, so `_processor_result_frame` — which *runs the whole task* —
finishes before `_send_processor_frame` sends the `R` byte. The parent's `_child_ready_handshake`
then records the descendants. The set it gets is therefore the tree the finished task left
running, which is exactly the tree that needs killing; it is not empty, on Windows or anywhere
else. `test_worker_cleans_grandchild_before_returning_normal_result` has been passing on that
behaviour all along.

What misled the audit — and this reader, at first — is the naming. `_child_ready_handshake` is
documented as "the child announces it is up, and only then do we track its tree", and `R` reads
as "ready". Both describe a start-up handshake this code does not have. A reader who believes
the docstring concludes that the set is always empty, and that conclusion is what the finding
records.

The window that genuinely is not covered is the one between that signal and the kill: a
great-grandchild spawned after the processor returned. It is unreachable from the product's own
path — the parent reads the frame and stops the child immediately — so nothing was added for it.
Containment does not depend on the set in any case: the child calls `os.setsid()` and POSIX kills
the whole process group, while Windows uses `taskkill /T`, which walks the OS's own parent-child
records.

## The macOS half, decided

`_process_snapshot_posix` reads `/proc`, which macOS does not have, so it returns `None` and
`_posix_process_pairs` degrades to `[]` — "no descendants" where the truth is "cannot say".
That asymmetry with Windows' `None` is real and is **left as it stands**:

- `_kill_posix_group` answers `descendants == set()` on its `ProcessLookupError` branch. With
  `None` that comparison is False, so every ordinary macOS timeout would report an unverified
  tree and `_cleanup_failed` would raise `process_cleanup_failed` — the defect M6 was fixed for
  in the previous round.
- macOS is not left unverified by the empty set. `_process_group_alive` falls back to
  `_process_group_alive_by_signal`, which asks the kernel directly about the group that was
  killed. `kill(2)` defines both halves of that: "If _pid_ is less than -1, then _sig_ is sent to
  every process in the process group whose ID is _-pid_", and "If _sig_ is 0, then no signal is
  sent, but existence and permission checks are still performed; this can be used to check for
  the existence of a process ID or process group ID that the caller is permitted to signal"
  ([Linux man-pages, kill(2)](https://man7.org/linux/man-pages/man2/kill.2.html)).
  The group is what was killed and the group is what is checked.

## The decision

- No behaviour is changed. The finding's premise does not reproduce, and the one uncovered window
  is not reachable from the worker's own path.
- `_send_processor_frame` and `_child_ready_handshake` now say what they actually do: `R` means
  "the task is finished and I have an answer", and the tree recorded on it is the one the task
  left running. Both note that moving the signal earlier would empty the set and leave
  `_cleanup_confirmed` and `_await_cleanup` verifying nothing.
- A test pins that ordering, so the next reader of these functions cannot quietly turn `R` into a
  start-up ping.
