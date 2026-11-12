# Shared helpers for scheduled maintenance scripts (nightly / weekly)

`scripts/maintenance_helpers.py` and the tests that exercise them most.

Both ``scheduled_nightly.py`` and ``scheduled_weekly.py`` need the same subprocess-step runner and compile-idle waiter. Extracted here to avoid the prior copy-paste duplication.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_log_many_writers_append_to_is_rotated_not_trimmed.py tests/test_a_report_link_outlives_no_report.py tests/test_maintenance_helpers.py tests/test_the_scheduler_log_is_bounded.py -q`.
