# Two failures at once both reach the trail

Dated 2026-09-17. Finding C-F11 of the third audit (low-medium, confirmed by reading). The
research before the fix.

## What was found

- `capture_diagnostics._append_failure_line` adds one line to `logs/capture-failures.jsonl` by
  reading every line, adding its own, trimming to the 256 KiB cap and writing the whole file
  back with `write_text`. No lock, no temporary file.
- Failures come in bursts — contention is what produces them — so two hooks doing this at the
  same moment is the normal case, not the rare one. Each reads the same old file and writes
  its own version; the later write erases the earlier line. A reader in between can see a
  file cut short. The counter beside it in `run/state.json` is updated under the state lock;
  the trail is not.

## Practice on this date

- Appending is the one file operation that is safe between processes without a lock:
  "O_APPEND — If set, the file offset shall be set to the end of the file prior to each write"
  ([POSIX.1-2017, open()](https://pubs.opengroup.org/onlinepubs/9699919799/functions/open.html)).
  One `write` of one short line lands whole and after every other line.
- Rewriting is not: it needs the writers to exclude each other, and the replacement to appear
  in one step (write beside, then rename). This repository already has both:
  `memory_state.update_state` for the exclusion, `memory_state.atomic_write` for the step.

## The decision

- A line is added with a single `O_APPEND` write. Nothing is read and nothing is rewritten on
  the ordinary path.
- The trail is trimmed only when it has grown past its cap, and only inside the same
  `update_state` call that already bumps the failure counter, through `atomic_write`. It is
  cut to three quarters of the cap, so trimming happens once per 64 KiB of failures rather
  than on every line.
- When the state lock cannot be taken the line is still appended, as before; only the trim
  waits for a later failure.

Files: `scripts/capture_diagnostics.py`, `tests/test_two_failures_at_once_both_reach_the_trail.py`
