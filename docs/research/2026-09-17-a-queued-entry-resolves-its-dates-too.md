# A queued entry resolves its dates too

Dated 2026-09-17. Part of finding C-F10 of the third audit (low-medium, confirmed by reading).
The research before the fix, and what of the finding is left to the owner.

## What was found

- On 2026-09-03 the daily-log writer was taught to follow an entry with the dates it mentions,
  resolved against the entry's day ("last Thursday" → an ISO date), because four of nine
  substantive refusals on the stand were a phrase and an anchor that nothing joined
  (`flush_memory._dated_block`, `docs/research/2026-09-03-a-calendar-of-what-happened.md`).
- That step lives in `flush_memory.append_daily`, the retired detached path. The live path —
  the capture worker — builds its block in `_capture_operation_plan` and commits it through
  the Markdown transaction, never through `append_daily`. So every entry written since the
  queue became the live path has no resolved dates. The fix exists only where nothing runs.
- The rest of the finding — a session captured late is filed under the worker's day, and a
  retry after midnight writes a second record under the next day — is real, but the obvious
  repair collides with consolidation: `episode_consolidation.pending_days` closes a day once
  it has been consolidated and never reads it again. A late record filed under its true,
  already-closed day would never reach a daily log, where today it is filed under the open
  day and is consolidated the next night. That needs one decision across capture and
  consolidation, and is left to the owner.

## Practice on this date

- Resolving a relative expression needs an anchor, and the anchor is the time the text was
  written: SUTime "will convert next wednesday at 3pm to something like 2016-02-17T15:00
  (depending on the assumed current reference time)"
  ([Stanford SUTime](https://nlp.stanford.edu/software/sutime.html), fetched 2026-09-17).
  The entry's own day is that reference time, and it is known at exactly one place: where the
  block is built.
- A rule that must hold for every writer belongs where every writer passes, not in one of two
  parallel paths.

## The decision

- `_capture_operation_plan` passes its block through `_dated_block` with the day it has just
  chosen, before the block's digest is taken. Stored decisions keep the block they recorded, so
  a replay writes what it wrote before.

Files: `scripts/flush_memory.py`, `tests/test_a_queued_entry_resolves_its_dates.py`
