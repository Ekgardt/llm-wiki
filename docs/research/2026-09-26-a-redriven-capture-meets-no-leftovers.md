# A redriven capture meets no leftovers, and its family does not stop the purge

Date: 2026-09-26. Audit 2026-09-26 A-12.

## Facts

- Read-only count on the live vault (`run/queue-v3.sqlite3`, 2026-09-26): 56
  `semantic_decisions` rows, 69 `capture-decision-*.json` files; 13 files are
  indexed by no row, and all 13 name a task that is `dead`.
- The decision file's path is keyed by intent and stage only
  (`flush_memory._capture_decision_relative_path`, and the queue validates the same
  key). A task that died after writing the file and before indexing it left the
  file; the redrive's child read it, failed `_require_capture_decision_identity`
  ("capture decision conflicts with its binding") and spent its one redrive.
- The weekly purge judged each capture row by its terminal proof. A redriven
  capture's terminal record binds the child that finished it, so the parent failed
  with `capture_terminal_invalid` and the whole purge raised; had the parent been
  merely kept, the child's purge evidence would still have failed
  `_require_capture_purge_unshared` (`capture_link_conflicted`) because both link
  one intent. Reproduced by `tests/test_a_redriven_capture_meets_no_leftovers.py`
  on the old code (both tests fail).
- The file path is part of the queue's validated contract; SQLite's documentation
  on the busy handler (https://www.sqlite.org/c3ref/busy_timeout.html, fetched
  2026-09-26) is unrelated here, so no storage-engine change is involved: the fix
  stays in the two readers.

## Decision

- `flush_memory._existing_capture_decision`: a decision file that no queue row
  indexes and whose `processing_binding.task_id` is not this task is a leftover of
  an earlier attempt. It is derived and unreferenced, so it is removed and the
  decision is made again. A file naming this task keeps the existing path
  (index-after-crash recovery).
- `MemoryQueue._capture_resolved`: a capture task in a redrive family (redriven,
  or a redrive itself) is kept and named in `retained`; the family is never purged
  half. Any other failed proof still refuses the purge.
- Honest limit: kept families stay in the queue; at most one redrive per capture
  bounds them.

## Files

- `scripts/flush_memory.py`
- `scripts/memory_queue.py`
- `tests/test_a_redriven_capture_meets_no_leftovers.py`
- `CHANGELOG.md`
