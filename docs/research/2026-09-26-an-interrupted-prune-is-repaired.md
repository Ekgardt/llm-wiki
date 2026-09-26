# An interrupted prune is repaired, not reported as damage

Date: 2026-09-26. Audit 2026-09-26 B-22 (C-16 confirmed).

## Facts

- `MarkdownCoordinator._prune_one` renames a transaction's images to
  `.<id>.pruning-<uuid>` before marking the row. A process that dies in between
  leaves that directory. Only `prune()` settled it (`_recover_interrupted_prunes`);
  `recover()`, which `doctor --repair` runs, did not.
- Doctor's `_transaction_artifacts` and `installed_memory_repair`'s artifact listing
  judged the dot-prefixed name an invalid artifact: doctor reported the trail
  unsafe and the runtime validator "transaction_state_unreadable", so an
  interrupted tidy-up looked like corruption and the repair did not clear it.
- SQLite's own answer to the same shape (https://www.sqlite.org/atomiccommit.html,
  fetched 2026-09-26): a hot journal is "our indication that a previous process was
  trying to commit a transaction but it aborted", and recovery "happens completely
  automatically and transparently" on the next access.

## Decision

- `recover()` settles interrupted prunes under the writer gate, as `prune()` does,
  so `doctor --repair` heals what doctor reports.
- Doctor lists a staged prune as its own kind and does not call the trail unsafe;
  the runtime validator names it `transaction_prune_interrupted` instead of
  "unreadable" (the backup still waits for it to be settled).

## Files

- `scripts/markdown_transaction.py`
- `scripts/doctor.py`
- `scripts/installed_memory_repair.py`
- `tests/test_an_interrupted_prune_is_repaired_not_reported_as_damage.py`
- `CHANGELOG.md`
