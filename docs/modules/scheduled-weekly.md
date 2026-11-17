# Weekly deep maintenance — started Sunday 04:00 by the installed scheduler

`scripts/scheduled_weekly.py`, `scripts/weekly_budget.py` and the tests that exercise them most.

The scheduler is the same one that starts the nightly pass (`install_control.py`). The nightly ran at 03:00, so the weekly does not repeat it; it runs only its own steps, in order: the OKF conformance sweep, a queue status report, the queue purge (finished work past its retention, exported to the private raw archive first), stale-page archiving, session-record archiving, daily-log archiving past the hot window, superseded-generation pruning, the opt-in contradiction check, A-MEM reflection and the L1 tier overviews.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_weekly_waits_for_the_nightly.py tests/test_scheduled_weekly.py tests/test_the_weekly_pass_has_its_own_record.py -q`.
