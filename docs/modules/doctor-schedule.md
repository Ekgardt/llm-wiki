# Doctor's checks of the code update, the weekly and the nightly passes

`scripts/llm_wiki/doctor_schedule.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_weekly_pass_counts_what_failed.py tests/test_a_weekly_that_never_finishes_is_seen.py tests/test_the_scheduler_says_what_the_night_could_not_do.py tests/test_the_scheduler_state_keeps_one_clock.py -q`.
