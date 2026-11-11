# Doctor's checks of the adopted queue, its owners and its dead work

`scripts/llm_wiki/doctor_queue.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_cut_in_the_core_says_what_it_left_out.py tests/test_a_failure_in_the_night_is_named.py tests/test_a_queue_health_scan_has_no_row_ceiling.py -q`.
