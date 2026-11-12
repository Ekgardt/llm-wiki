# Reclaim what the hook path is too impatient to finish

`scripts/reclaim_runtime_state.py` and the tests that exercise them most.

Two jobs, both of them recovery rather than routine work, and both of them impossible from a session hook:

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_killed_prune_neither_blocks_undo_nor_outruns_its_step.py tests/test_a_killed_write_leaves_nothing_behind.py tests/test_a_spent_attempt_keeps_its_state.py tests/test_a_sweep_says_what_it_could_not_do.py tests/test_empty_ready_shards_are_removed.py -q`.
