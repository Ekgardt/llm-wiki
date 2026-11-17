# Tests of `scripts/llm_wiki/doctor_capture.py`

- `tests/test_a_checkpoint_that_never_reached_the_database_is_seen.py`: A checkpoint still queued after a nightly drain is reported (audit 2026-09-26 C-12).
- `tests/test_a_lost_race_is_not_a_hook_failure.py`: A writer that lost a race did not fail: the next session carries the event.
- `tests/test_hook_failures_reach_health.py`: The hook failure trail was written by the product and read by nobody.

`scripts/llm_wiki/doctor_capture.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_checkpoint_that_never_reached_the_database_is_seen.py tests/test_a_lost_race_is_not_a_hook_failure.py tests/test_hook_failures_reach_health.py -q`.
