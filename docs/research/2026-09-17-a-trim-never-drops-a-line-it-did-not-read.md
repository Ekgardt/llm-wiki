# A trim never drops a line it did not read

Dated 2026-09-17. The remainder of finding C-F11 of the third audit, found again in the
second round: the trail's append was made safe, its trim was not.

## What was found

- `capture_diagnostics.record_capture_failure` writes the failure line under the state lock
  and trims the trail there too. When the state lock is held by another writer it falls back
  to an append with no lock and no trim (`_trail_written(record, trim=False)`), which was the
  2026-09-17 fix for two hooks failing at the same moment.
- `_trim_failure_log` is still a read of every line (`_existing_lines`) followed by a whole-file
  replace (`atomic_write`). Nothing links the two writers: a fallback append that lands between
  that read and that replace is erased by the replace. The counter in `run/state.json` still
  counts the failure, so the loss is bounded to the trail, but the trail is where the reason is.
- The Windows byte lock added the same day serializes two appenders against each other. It
  cannot help here: it is taken on the trail's own inode, and a trim replaces that inode by
  rename, so an appender can hold the lock on a file that is already unlinked.

## Practice on this date

- A lock that has to survive a rename belongs on a sidecar file, not on the file being replaced.
  The vault already uses that shape for `run/state.json` (`state.json.lock`).
- The lock has to be taken by both sides on descriptors of their own: "If a process uses open(2)
  (or similar) to obtain more than one file descriptor for the same file, these file descriptors
  are treated independently by flock(). An attempt to lock the file using one of these file
  descriptors may be denied by a lock that the calling process has already placed via another
  file descriptor" ([flock(2), man7.org](https://man7.org/linux/man-pages/man2/flock.2.html),
  fetched 2026-09-17). So a test can hold the lock and watch the product refuse to trim, without
  a second process.
- `flock()` "places advisory locks only" (same page): every writer of this trail is ours, which
  is what makes an advisory lock sufficient here.

## The decision

- One sidecar lock, `logs/capture-failures.jsonl.lock`, guards the trail: `msvcrt.locking` on
  Windows, `fcntl.flock` on POSIX, non-blocking with the bounded retry the trail already uses
  (20 × 25 ms).
- `_trim_failure_log` trims only while it holds that lock. If it cannot take it, it leaves the
  trail alone; the next recorded failure trims instead. A trail one quarter over its cap for a
  few seconds costs nothing.
- `_append_failure_line` takes the same lock around opening and writing the trail, so an append
  never lands in an inode a trim has already replaced. If the lock is not free inside the bound
  the line is written anyway: a diagnostic line lost to a lock would be the defect this trail
  exists to prevent, and the residual window is one starved appender against one trim.

Files: `scripts/capture_diagnostics.py`,
`tests/test_a_trim_never_drops_a_line_it_did_not_read.py`
