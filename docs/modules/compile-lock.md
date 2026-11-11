# The on-disk lock that keeps one compile running at a time

`scripts/compile_lock.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_spawned_compile_knows_its_lock.py tests/test_one_bad_day_does_not_hold_the_rest.py tests/test_the_small_integrity_gaps.py -q`.
