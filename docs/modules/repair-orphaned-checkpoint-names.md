# Re-attempt checkpoints whose names no request can produce any more

`scripts/repair_orphaned_checkpoint_names.py` and the tests that exercise them most.

A quarantined or reserved checkpoint sequence blocks every sequence behind it, and the design clears it the right way: the original request arrives again, re-derives the same `occurrence_id`, and `_retry_spent_checkpoint` turns the row into a fresh attempt. `tests/test_project_journal.py` states that contract in seven tests and nothing here changes it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_live_reservation_is_not_reclaimed.py tests/test_a_vanished_project_is_rebuilt_by_the_night.py tests/test_orphaned_checkpoint_names_are_repaired.py -q`.
